# Personal RAG Data Management & Ontology Architecture (v2)

This document dictates strict data governance. The objective is to build a human-readable Knowledge Graph using highly modular, atomic files. We absolutely MUST avoid "mega-files" to ensure fast querying and low token consumption.

## 1. The Internal Structure of the Wiki (Database Layout)

Inside the `memory_bank/` (which acts as your LLM-maintained Wiki layer), this system mirrors a relational database broken down into horizontal tiers:

### Tier 1: The Routing Indices (The Pointers)
These files do not contain raw data. They exist purely as tables of contents.
- `index.md` (Master Router)
- `projects/index.projects.md` (List of all projects with a 1-sentence summary and a `[[WikiLink]]` to the project node)
- `entities/index.entities.md`

### Layer 2: The Atomic Nodes (The Hubs)
A node focuses on one specific subject. If a node exceeds 500 words, it MUST be fractured into sub-nodes.
- *Example:* Instead of one massive `projects/VoiceFlow.md` containing everything, we create:
    - `projects/VoiceFlow_Overview.md`
    - `projects/VoiceFlow_TechStack.md`
    - `projects/VoiceFlow_Lessons.md`
- These atomic nodes link to each other. This ensures that when an AI retrieves data, it only pulls the specific 300 tokens it needs (e.g., just the TechStack), saving massive token costs.

### Layer 3: The Raw Archive (The Ground Truth)
We do NOT duplicate large source files (like massive PDFs or 10,000 line codebases) into the wiki. 
- The wiki nodes simply store the **Absolute File Path** to the raw data.
- The AI only reads the raw data if explicitly commanded to do a deep dive.

## 2. Key Buckets (The Directory Taxonomy)

```text
memory_bank/
├── indices/                     # Layer 1 files
├── entities/                    # Atomic nodes for People, Companies
├── chronology/                  # Atomic nodes for daily logs / specific events
├── ideologies/                  # Atomic nodes for specific philosophies
└── tech_portfolio/              # Atomic nodes for individual projects and repos
```

## 3. Token-Efficient Ingestion Strategy (For Phase 1)

**Rule: Never blind-read entire directories.** 
When crawling Jay's computer to ingest historical projects, the AI will use a high-efficiency scanning protocol:
1. **Shallow Scan:** Run `list_dir` on a project folder. Look ONLY at the directory tree.
2. **Metadata Extraction:** Read ONLY the `README.md`, `package.json`, or high-level architecture files. 
3. **Drafting:** Generate the Atomic Nodes (Layer 2) based merely on the summaries. 
4. **Deep Dive (On-Demand Only):** The AI will *never* read internal source code (`.py`, `.ts` files) unless Jay explicitly asks: "Extract the exact LangGraph implementation from my GraphRAG project."
