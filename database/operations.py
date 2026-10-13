"""Database operations module for handling database interactions.

This module provides functions for saving and retrieving search queries
and generated pages from the MongoDB database.
"""

from datetime import datetime, timedelta
from typing import Optional, List, Dict
from .connection import search_queries, generated_pages
from .models import SearchQuery, GeneratedPage

def save_search_query(query: str, results: List[Dict]) -> None:
    """Save a search query and its results"""
    search_query = SearchQuery(query, results)
    search_queries.insert_one(search_query.to_dict())

def get_recent_search_results(query: str, max_age_minutes: int = 30) -> Optional[List[Dict]]:
    """Get cached search results if they exist and are recent"""
    cutoff_time = datetime.utcnow() - timedelta(minutes=max_age_minutes)
    result = search_queries.find_one({
        'query': query,
        'timestamp': {'$gt': cutoff_time}
    }, sort=[('timestamp', -1)])
    return result['results'] if result else None

def save_generated_page(url: str, content: str) -> None:
    """Save or update a generated page"""
    page = GeneratedPage(url, content)
    generated_pages.update_one(
        {'url': url},
        {'$set': page.to_dict()},
        upsert=True
    )

def get_generated_page(url: str) -> Optional[str]:
    """Get a generated page by URL"""
    page = generated_pages.find_one({'url': url})
    return page['content'] if page else None

def get_search_history(limit: int = 10) -> List[Dict]:
    """Get recent search history.
    
    Args:
        limit (int): Maximum number of searches to return
        
    Returns:
        List[Dict]: List of recent searches with their timestamps
    """
    history = search_queries.find(
        {},
        {'query': 1, 'timestamp': 1, '_id': 0}
    ).sort('timestamp', -1).limit(limit)
    
    return list(history)
