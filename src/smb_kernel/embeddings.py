"""The embedding value every adapter returns.

The configured adapter itself is `smb_kernel.llm.compatible_transport.ConfiguredKnowledgeEmbedding`,
because it shares that module's provider transport. What is embedded, and how
vectors are searched, is each application's retrieval behaviour.
"""

type Embedding = tuple[float, ...]
