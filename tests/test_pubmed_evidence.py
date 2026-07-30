"""Tests for the PubMed evidence integration.

Covers:
- efetch XML parsing into article dicts
- search_evidence never raises (network/parse errors -> [])
- evidence-query detection + topic extraction
- deterministic source citation formatting
- _generate_evidence_reply appends sources and honors the no-results path
"""
from unittest.mock import patch, MagicMock

SAMPLE_XML = """<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>12345678</PMID>
      <Article>
        <ArticleTitle>Training to failure and muscle hypertrophy: a meta-analysis</ArticleTitle>
        <Abstract>
          <AbstractText Label="BACKGROUND">Resistance training stimulates growth.</AbstractText>
          <AbstractText Label="RESULTS">Training near failure maximized hypertrophy.</AbstractText>
        </Abstract>
        <Journal>
          <ISOAbbreviation>J Strength Cond Res</ISOAbbreviation>
          <JournalIssue><PubDate><Year>2021</Year></PubDate></JournalIssue>
        </Journal>
      </Article>
    </MedlineCitation>
  </PubmedArticle>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>87654321</PMID>
      <Article>
        <ArticleTitle>Protein timing and lean mass</ArticleTitle>
        <Abstract><AbstractText>Total daily protein matters most.</AbstractText></Abstract>
        <Journal>
          <Title>Nutrients</Title>
          <JournalIssue><PubDate><Year>2019</Year></PubDate></JournalIssue>
        </Journal>
      </Article>
    </MedlineCitation>
  </PubmedArticle>
</PubmedArticleSet>"""


class TestParseArticles:
    def test_parses_titles_abstracts_and_links(self):
        from fitnessbot import pubmed
        arts = pubmed.parse_articles(SAMPLE_XML)
        assert len(arts) == 2
        a = arts[0]
        assert a["pmid"] == "12345678"
        assert a["title"].startswith("Training to failure")
        assert "BACKGROUND:" in a["abstract"] and "RESULTS:" in a["abstract"]
        assert a["journal"] == "J Strength Cond Res"
        assert a["year"] == "2021"
        assert a["url"] == "https://pubmed.ncbi.nlm.nih.gov/12345678/"
        assert arts[1]["journal"] == "Nutrients"

    def test_skips_articles_missing_pmid_or_title(self):
        from fitnessbot import pubmed
        xml = """<PubmedArticleSet><PubmedArticle><MedlineCitation>
          <Article><ArticleTitle>No PMID here</ArticleTitle></Article>
        </MedlineCitation></PubmedArticle></PubmedArticleSet>"""
        assert pubmed.parse_articles(xml) == []


class TestSearchEvidence:
    def test_returns_empty_on_network_error(self):
        from fitnessbot import pubmed
        with patch("fitnessbot.pubmed.search_pmids", side_effect=RuntimeError("boom")):
            assert pubmed.search_evidence("creatine") == []

    def test_empty_query_short_circuits(self):
        from fitnessbot import pubmed
        assert pubmed.search_evidence("   ") == []

    def test_happy_path(self):
        from fitnessbot import pubmed
        with patch("fitnessbot.pubmed.search_pmids", return_value=["1", "2"]) as ms, \
             patch("fitnessbot.pubmed.fetch_articles", return_value=[{"pmid": "1"}]):
            out = pubmed.search_evidence("training to failure", max_results=2)
        ms.assert_called_once_with("training to failure", retmax=2)
        assert out == [{"pmid": "1"}]


class TestEvidenceDetection:
    def test_detects_research_questions(self):
        from fitnessbot.bot.conversation import _is_evidence_query
        assert _is_evidence_query("what does the research say about training to failure?")
        assert _is_evidence_query("is there evidence for creatine?")
        assert _is_evidence_query("any studies on intermittent fasting")
        assert _is_evidence_query("what does the science say about sleep and recovery")
        assert not _is_evidence_query("how am I doing today")
        assert not _is_evidence_query("I ate a chicken breast")

    def test_fast_path_routes_to_query(self):
        from fitnessbot.bot.conversation import _fast_path_intents
        intents = _fast_path_intents("is there research on creatine for strength?", None)
        assert intents == [{"type": "query", "question": "is there research on creatine for strength?", "confidence": 0.9}]

    def test_topic_extraction_strips_leadins(self):
        from fitnessbot.bot.conversation import _extract_evidence_topic
        assert _extract_evidence_topic("what does the research say about training to failure?") == "training to failure"
        assert _extract_evidence_topic("is there evidence for creatine?") == "creatine"
        assert _extract_evidence_topic("any studies on intermittent fasting") == "intermittent fasting"
        # No recognizable lead-in -> return the cleaned question
        assert _extract_evidence_topic("creatine monohydrate benefits") == "creatine monohydrate benefits"


class TestSourceFormatting:
    def test_formats_numbered_sources_with_links(self):
        from fitnessbot.bot.conversation import _format_evidence_sources
        arts = [
            {"title": "Study A", "journal": "J Test", "year": "2020", "url": "https://pubmed.ncbi.nlm.nih.gov/1/"},
            {"title": "Study B", "journal": "", "year": "", "url": "https://pubmed.ncbi.nlm.nih.gov/2/"},
        ]
        out = _format_evidence_sources(arts)
        assert "[1] Study A — J Test (2020)" in out
        assert "https://pubmed.ncbi.nlm.nih.gov/1/" in out
        assert "[2] Study B" in out
        assert "https://pubmed.ncbi.nlm.nih.gov/2/" in out


class TestGenerateEvidenceReply:
    def test_no_articles_returns_helpful_message(self):
        from fitnessbot.bot import conversation
        with patch("fitnessbot.pubmed.search_evidence", return_value=[]):
            text, tokens = conversation._generate_evidence_reply(1, "research on unobtainium", "neutral", "")
        assert "couldn't pull up studies" in text.lower()
        assert tokens == {"input_tokens": 0, "output_tokens": 0}

    def test_appends_sources_after_llm_answer(self):
        from fitnessbot.bot import conversation
        arts = [{"title": "Failure training", "journal": "JSCR", "year": "2021",
                 "abstract": "Training near failure grows muscle.",
                 "url": "https://pubmed.ncbi.nlm.nih.gov/99/", "pmid": "99"}]
        fake_infer = MagicMock(return_value={"text": "Train close to failure [1].",
                                             "input_tokens": 10, "output_tokens": 5})
        with patch("fitnessbot.pubmed.search_evidence", return_value=arts), \
             patch("fitnessbot.inference.factory.get_inference", return_value=fake_infer):
            text, tokens = conversation._generate_evidence_reply(1, "what does research say about failure?", "neutral", "")
        assert "Train close to failure [1]." in text
        assert "Sources (PubMed)" in text
        assert "https://pubmed.ncbi.nlm.nih.gov/99/" in text
        assert tokens["input_tokens"] == 10

    def test_llm_failure_falls_back_but_still_cites(self):
        from fitnessbot.bot import conversation
        from fitnessbot.inference.base import InferenceError
        arts = [{"title": "Study X", "journal": "J", "year": "2020",
                 "abstract": "abc", "url": "https://pubmed.ncbi.nlm.nih.gov/5/", "pmid": "5"}]
        fake_infer = MagicMock(side_effect=InferenceError("no key"))
        with patch("fitnessbot.pubmed.search_evidence", return_value=arts), \
             patch("fitnessbot.inference.factory.get_inference", return_value=fake_infer):
            text, _ = conversation._generate_evidence_reply(1, "evidence on X", "neutral", "")
        assert "research turned up" in text.lower()
        assert "https://pubmed.ncbi.nlm.nih.gov/5/" in text
