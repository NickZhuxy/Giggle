"""Database models module for defining data structures.

This module contains the data models used for storing search queries
and generated pages in the MongoDB database.
"""

from datetime import datetime
from typing import List, Dict, Any


class SearchQuery:
    """Represents a search query and its results in the database.
    
    Attributes:
        query (str): The search query string
        results (List[Dict[str, Any]]): The search results
        timestamp (datetime): When the query was made
    """
 
    def __init__(self, query: str, results: List[Dict[str, Any]]):
        """Initialize a new search query.
        
        Args:
            query (str): The search query string
            results (List[Dict[str, Any]]): The search results
        """
        self.query = query
        self.results = results
        self.timestamp = datetime.utcnow()

    def to_dict(self) -> Dict:
        """Convert the search query to a dictionary for database storage.
        
        Returns:
            Dict: Dictionary representation of the search query
        """
        return {
            'query': self.query,
            'results': self.results,
            'timestamp': self.timestamp
        }


class GeneratedPage:
    """Represents a generated page in the database.
    
    Attributes:
        url (str): The URL of the generated page
        content (str): The HTML content of the page
        generated_at (datetime): When the page was generated
    """
 
    def __init__(self, url: str, content: str):
        """Initialize a new generated page.
        
        Args:
            url (str): The URL of the page
            content (str): The HTML content of the page
        """
        self.url = url
        self.content = content
        self.generated_at = datetime.utcnow()

    def to_dict(self) -> Dict:
        """Convert the generated page to a dictionary for database storage.
        
        Returns:
            Dict: Dictionary representation of the generated page
        """
        return {
            'url': self.url,
            'content': self.content,
            'generated_at': self.generated_at
        }
