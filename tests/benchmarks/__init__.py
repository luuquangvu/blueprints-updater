"""Performance benchmarks for the Blueprints Updater integration.

These benchmarks measure key hot paths including:
- YAML parsing and semantic normalization
- Hash computation and content comparison
- Input schema extraction and risk detection
- Provider URL resolution and normalization
- Data structure operations (dict merges, filtering)
- Backup file operations
- Concurrency patterns and locking
"""
