import json

from enrichment import EnrichingMatcher, LeadEnricher, extract_html_text
from matcher import MatchResult


def test_extract_html_text_decodes_nextjs_streamed_job_copy():
    html = '''
    <html><head><title>Talk Machine</title></head><body>
    <div>Join me on Talk Machine</div>
    <script>self.__next_f.push([1,"Hello. We’re looking for founding engineers to join our small technical team building a voice-first product.\\n\\nWork across backend APIs, audio and transcription systems, and AI agent workflows."])</script>
    </body></html>
    '''
    text = extract_html_text(html)
    assert "voice-first product" in text
    assert "audio and transcription systems" in text
    assert "static/chunks" not in text


def test_lead_enricher_uses_cached_primary_evidence(tmp_path):
    enricher = LeadEnricher(cache_dir=tmp_path)
    url = "https://talkmachine.com/jobs/engineer"
    cache = enricher._cache_path(url)
    cache.write_text(json.dumps({
        "original_description": "HN lead",
        "primary_url": url,
        "primary_text": "Voice AI founding engineer role",
        "primary_content_hash": "abc123",
        "primary_fetched_at": "2026-07-16T12:00:00+00:00",
        "primary_fetch_method": "http",
    }))

    result = enricher.enrich("HN lead", url)
    assert "PRIMARY ADVERT PAGE" in result.matcher_description
    assert result.primary_content_hash == "abc123"


def test_enriching_matcher_passes_primary_page_to_matcher(tmp_path):
    class FakeEnricher:
        def enrich(self, description, url):
            from enrichment import EnrichedLead
            return EnrichedLead(
                original_description=description,
                primary_url=url,
                primary_text="Voice, speech recognition, AI agents, small team",
                primary_content_hash="feedface",
                primary_fetched_at="2026-07-16T12:00:00+00:00",
                primary_fetch_method="http",
            )

    class FakeMatcher:
        def __init__(self):
            self.description = ""

        def match(self, title, description, org, job_url="", location=""):
            self.description = description
            return MatchResult(score=8, decision="GO", reasoning="direct match")

    inner = FakeMatcher()
    result = EnrichingMatcher(inner, FakeEnricher()).match(
        "Founding Engineer", "HN snippet", "Talk Machine",
        job_url="https://talkmachine.com/jobs/engineer",
    )
    assert "speech recognition" in inner.description
    assert result.primary_content_hash == "feedface"
