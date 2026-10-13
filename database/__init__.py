"""Database package for MongoDB integration.

This package provides database models, connection handling, and operations
for the Giigle search engine application.
"""

from .connection import init_db
from .models import SearchQuery, GeneratedPage
from .operations import (
    save_search_query,
    get_recent_search_results,
    save_generated_page,
    get_generated_page
)

__all__ = [
    'init_db',
    'SearchQuery',
    'GeneratedPage',
    'save_search_query',
    'get_recent_search_results',
    'save_generated_page',
    'get_generated_page'
] 