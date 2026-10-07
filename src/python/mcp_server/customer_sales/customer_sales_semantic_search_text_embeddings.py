#!/usr/bin/env python3
"""
Customer Sales Semantic Search Tool

This module provides semantic search functionality for products using Azure OpenAI embeddings.
It generates embeddings for user queries and finds similar products using pgvector cosine similarity.

Usage:
    from customer_sales_semantic_search import SemanticSearchTool
    
    tool = SemanticSearchTool()
    embedding = tool.generate_query_embedding("waterproof electrical box")

Requirements:
    - Azure OpenAI configured
    - openai package
    - azure-identity package
"""

import os
from pathlib import Path
from typing import List, Optional

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from dotenv import load_dotenv
from openai import AzureOpenAI


class SemanticSearchTextEmbedding:
    """Handles semantic search operations using Azure OpenAI embeddings."""
    
    def __init__(self) -> None:
        """Initialize the semantic search tool with Azure OpenAI configuration."""
        # Load environment variables
        self._load_environment()
        
        # Azure OpenAI configuration
        self.endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "<ENDPOINT_URL>")
        self.model_name = "text-embedding-3-small"
        self.deployment = os.getenv("EMBEDDING_MODEL_DEPLOYMENT_NAME", "text-embedding-3-small")
        
        # Check if Azure OpenAI endpoint is configured
        if self.endpoint == "<ENDPOINT_URL>":
            print("Warning: AZURE_OPENAI_ENDPOINT not configured. Semantic search will not work.")
            self.openai_client = None
            return
        
        # Initialize Azure OpenAI client
        try:
            self.openai_client = self._setup_azure_openai_client()
        except Exception as e:
            print(f"Failed to initialize Azure OpenAI client: {e}")
            self.openai_client = None
    
    def _load_environment(self) -> None:
        """Load environment variables from .env files."""
        script_dir = Path(__file__).parent
        # Try to load .env from script directory first, then parent directories
        env_paths = [
            script_dir / '.env',
            script_dir.parent.parent.parent / '.env',  # Up to workspace root
        ]

        for env_path in env_paths:
            if env_path.exists():
                load_dotenv(env_path)
                break
        else:
            # Fallback to default behavior
            load_dotenv()
    
    def _setup_azure_openai_client(self) -> AzureOpenAI:
        """Setup and return Azure OpenAI client with token provider."""
        token_provider = get_bearer_token_provider(
            DefaultAzureCredential(), 
            "https://cognitiveservices.azure.com/.default"
        )
        api_version = "2024-02-01"
        
        return AzureOpenAI(
            api_version=api_version,
            azure_endpoint=self.endpoint,
            azure_ad_token_provider=token_provider,
        )
    
    def generate_query_embedding(self, query_text: str) -> Optional[List[float]]:
        """
        Generate embedding for the user's query text.

        Args:
            query_text: The user's product description query

        Returns:
            List of float values representing the embedding, or None if failed
        """
        embeddings = self.generate_query_embeddings([query_text])
        return embeddings[0] if embeddings else None

    def generate_query_embeddings(self, query_texts: List[str]) -> Optional[List[List[float]]]:
        """
        Generate embeddings for several query texts in one API call.

        The embeddings endpoint accepts a batch, so a search covering twenty
        materials costs one round trip instead of twenty.

        Args:
            query_texts: The user's product description queries

        Returns:
            One embedding per query, in the order given, or None if failed
        """
        if not self.openai_client:
            print("Azure OpenAI client not initialized. Cannot generate embeddings.")
            return None

        if not query_texts:
            return None

        try:
            print(f"Generating embeddings for {len(query_texts)} query/queries: {query_texts}")

            # Generate embeddings using Azure OpenAI
            response = self.openai_client.embeddings.create(
                input=list(query_texts),
                model=self.deployment
            )

            # Sort by index before unpacking: the caller relies on the results
            # lining up positionally with the queries it passed in.
            ordered = sorted(response.data, key=lambda item: item.index)
            embeddings = [item.embedding for item in ordered]

            if len(embeddings) != len(query_texts):
                print(f"Error: expected {len(query_texts)} embeddings, got {len(embeddings)}")
                return None

            print(f"✓ Generated {len(embeddings)} embedding(s) (dimension: {len(embeddings[0])})")
            return embeddings

        except Exception as e:
            print(f"Error generating embedding: {e}")
            return None
    
    def is_available(self) -> bool:
        """Check if the semantic search functionality is available."""
        return self.openai_client is not None