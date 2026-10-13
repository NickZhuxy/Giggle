"""Database connection module for MongoDB setup and initialization.

This module handles the MongoDB connection setup, database initialization,
and provides access to the collections used throughout the application.
"""

import os
from pymongo import MongoClient
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Get MongoDB connection string from environment variable
MONGO_URI = os.getenv('MONGO_URI', 'mongodb://localhost:27017/fake_google')

# Create a MongoDB client
client = MongoClient(MONGO_URI)
# Get database name from URI or use default
db_name = MONGO_URI.split('/')[-1] if '/' in MONGO_URI else 'fake_google'
db = client[db_name]

# Collections
search_queries = db.search_queries
generated_pages = db.generated_pages

def init_db():
    """Initialize database with indexes"""
    # Create indexes for better query performance
    search_queries.create_index('query')
    search_queries.create_index('timestamp')
    generated_pages.create_index('url', unique=True)
    generated_pages.create_index('generated_at')
