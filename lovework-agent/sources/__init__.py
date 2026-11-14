"""Source modules for lovework-agent."""

from .research_orgs import ResearchOrgsSource
from .neolabs import NeolabsSource
from .neoclouds import NeocloudsSource
from .speech_companies import SpeechCompaniesSource
from .market_players import MarketPlayersSource
from .hf_startups import HFStartupsSource
from .hn_hiring import HNHiringSource
from .hn_jobs import HNHiringJobsSource
from .gmail_lj_jobs import GmailLjJobsSource
from .linkedin_related import LinkedInRelatedSource
from .company_pages import CompanyPagesSource
from .harnham import HarnhamSource
from .digitaljobs import DigitalJobsSource
from .jackandjill import JackAndJillSource

__all__ = [
    "ResearchOrgsSource",
    "NeolabsSource",
    "NeocloudsSource",
    "SpeechCompaniesSource",
    "MarketPlayersSource",
    "HFStartupsSource",
    "HNHiringSource",
    "HNHiringJobsSource",
    "GmailLjJobsSource",
    "LinkedInRelatedSource",
    "CompanyPagesSource",
    "HarnhamSource",
    "DigitalJobsSource",
    "JackAndJillSource",
]
