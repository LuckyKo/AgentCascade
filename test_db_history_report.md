# Evolution of Database Systems: From Hierarchical to Vector/Graph

**Prepared by:** researcher (test_dismiss_live)
**Date:** 2026-08-10
**Objective:** Document the evolution of database systems across five major eras, citing multiple sources per era.
**Confidence:** High (multi-source cross-checking per era; see per-section notes)

---

## 1. Hierarchical & Navigational Databases (1960s)

### 1.1 IBM IMS — the hierarchical model in production
- **Development began 1966** at IBM to track the bill of materials for the **Saturn V rocket / Apollo program**; first version completed 1967 on the IBM System/360 Model 65. [Wikipedia: IBM Information Management System; IBM History: Information Management System]
- Commercial product = database manager (hierarchical data model) + software for high-volume transaction processing (e.g., ATMs, travel reservations). IBM launched a Database/Data Communications line with IMS as the backbone. [IBM History]
- Data is organized as **trees**; applications must traverse parent–child pointer paths to find records (navigational access). [Enginerds course: "A Brief History of Database Systems"]

### 1.2 CODASYL — the network model
- **CODASYL** (Conference/Committee on Data Systems Languages) formed **1959** to standardize a common programming language (origin of COBOL); extended its scope to database standards in the late 1960s. [Wikipedia: CODASYL]
- The **network model** (CODASYL DBTG) generalized hierarchies into **graphs** with sets/links; data access remained **navigational** (application code follows pointers from record to record). [Enginerds course; substack: "IMS, CODASYL & CODD's relational model"]

### 1.3 Why they mattered / limitations
- Engineering marvels for their era, but queries were entangled with physical storage structure — programmers had to know pointer layouts, making data access rigid and hard to evolve. [Substack: "IMS, CODASYL & CODD's relational model — what came before SQL"]
- This tight coupling to physical structure is the key contrast with the next era's logical/physical separation.

---

## 2. The Relational Model & RDBMS (1970s–1990s)

### 2.1 Codd's 1970 paper
- **Edgar F. "Ted" Codd** (IBM, mathematician; 1981 ACM Turing Award) published **"A Relational Model of Data for Large Shared Data Banks"** (1970, ~11 pages). [IBM History: Edgar F. Codd; Wikipedia: Edgar F. Codd]
- Core idea: data organized in **tables (relations)**; users access data by **logical description, not physical layout** — no knowledge of the physical blueprint required. [IBM History: The relational database]

### 2.2 System R and SQL
- **IBM System R** research project at San Jose Research Laboratory, **beginning 1974**, implemented Codd's ideas. [Wikipedia: IBM System R]
- System R produced **SEQUEL/SQL** and demonstrated that relational systems could perform acceptably — the foundation of the commercial RDBMS industry.

### 2.3 Commercialization
- The relational model spawned the multibillion-dollar database industry (Oracle, IBM DB2, Microsoft SQL Server, later open-source PostgreSQL and MySQL). [IBM History: Edgar F. Codd; IBM History: relational database]
- SQL became the standard interface; the RDBMS dominated enterprise data management from the 1980s onward.

### Sources (Tier 1–2)
- IBM History (tier 1), Wikipedia (tier 2), ACM award context (tier 5/academic).
- **Confidence: Confirmed** — well-documented, consistent across independent sources.

---

## 3. NoSQL (late 1990s–2010s)

### 3.1 Origin of the term
- **1998:** Carlo Strozzi coined "NoSQL" for his lightweight open-source **relational** database that did not expose SQL (i.e., "no SQL" literally). [Meegle; dev.to; Martin Fowler blog]
- **June 11, 2009:** Johan Oskarsson organized an SF meetup; Eric Evans and Oskarsson repurposed "NoSQL" to describe **non-relational, distributed databases**. [Martin Fowler: NosqlDefinition; Meegle; Eduzan]
- Fowler notes there is "no strong definition, no trademark, no manifesto" — it's a loose umbrella term. [Martin Fowler blog]

### 3.2 Pioneering systems and papers
- **Google Bigtable** paper (2006): distributed storage for large-scale structured data; inspired HBase, Cassandra.
- **Amazon Dynamo** paper (2007): argued distributed environments require **eventual consistency instead of ACID** — became NoSQL's theoretical backbone. [IT History: MongoDB 1.0]
- **Facebook Cassandra (2008):** combines Bigtable's data model with Dynamo's distributed architecture. [Meegle]
- **MongoDB 1.0 (2009):** document-oriented flagbearer of the movement. [IT History: MongoDB 1.0]

### 3.3 Drivers and characteristics
- Web 2.0 scale, unstructured data, horizontal scalability, schema flexibility; families: key-value (Redis, DynamoDB), document (MongoDB, Couchbase), wide-column (HBase, Cassandra), and later graph. [Wikipedia: NoSQL; dev.to]
- Trade-off: relaxed consistency (CAP trade-offs) in exchange for availability and partition tolerance. [IT History]

### Sources (Tier 1–4)
- Martin Fowler (tier 4), vendor/IT history blogs (tier 4), Wikipedia (tier 2).
- **Confidence: High** — term-origin story is consistent (1998 Strozzi; 2009 Oskarsson/Evans) across independent sources.

---

## 4. NewSQL (2011–present)

### 4.1 Term origin and definition
- **"NewSQL" coined in 2011 by Matthew Aslett** of the 451 Group (451 Research) in a business analysis report describing a new generation of RDBMSs that **preserve ACID and SQL while achieving NoSQL-like scalability**. [HandWiki; Wikiward; ACM SIGMOD Record "What's Really New with NewSQL?" — co-authored by Aslett]
- One of the first NewSQL systems was **H-Store** (MIT/Brown parallel in-memory OLTP research project). [HandWiki; Wikiward]

### 4.2 Commercial and research exemplars
- **VoltDB:** NewSQL OLTP relational database, SQL via pre-compiled Java stored procedures. [Wikipedia: VoltDB]
- **Google Spanner (OSDI 2012):** "scalable, multi-version, globally-distributed, synchronously-replicated database; the first system to distribute data at global scale and support externally-consistent distributed transactions" via the novel **TrueTime** clock API. [Google Research; USENIX OSDI 2012 paper; ACM DL]
- **Google F1:** distributed relational DB built on Spanner for **AdWords**, replacing an overloaded MySQL cluster; combines NoSQL scalability with SQL consistency/ACID. [Google Research: F1; dbdb.io]
- Modern lineage continues: Spanner is now a managed cloud service "bringing together relational, graph, key-value, and search" with GoogleSQL/PostgreSQL dialects. [Google Cloud Spanner docs]

### Sources (Tier 1–3)
- OSDI 2012 paper + USENIX/ACM (tier 1–3), Google Research (tier 1), SIGMOD Record (tier 3), Wikipedia/handwiki (tier 2).
- **Confidence: Confirmed** — term origin (Aslett, 2011) and Spanner paper details verified via primary literature.

---

## 5. Modern Eras: Graph & Vector Databases

### 5.1 Graph databases (2000s–present)
- Two lineages: **RDF/triple stores** (descended from Tim Berners-Lee's **Semantic Web**, formal logic/knowledge representation, standardized for data integration and reasoning) and **labeled property graphs** (database-native, optimized for traversal — Neo4j, Apache TinkerPop/Gremlin). [Neo4j blog; Talisman substack; adhdecode]
- **Neo4j:** graph DBMS implemented in Java; company founded as Neo Technology (2007); **Cypher** declarative graph query language largely invented by **Andrés Taylor (2011)**; opened via **openCypher project in October 2015**. [Wikipedia: Neo4j; Wikipedia: Cypher (query language)]
- Property graphs were "designed as a database model... for applications and analytics," whereas RDF was designed as a data-exchange format. [Neo4j blog: RDF vs property graphs]

### 5.2 Vector databases (2017–present)
- **FAISS (Facebook AI Similarity Search), 2017:** open-source library (Meta AI) for efficient similarity search/clustering on dense vectors — algorithms, not a full DBMS; the conceptual origin of the vector-DB wave. [SwiftTools blog; bigdataclouds; arXiv 2401.08281 "The Faiss Library"]
- **Purpose-built vector DBs** followed: **Pinecone (managed SaaS)**, **Milvus (open-source, self-hostable)**, Chroma, Qdrant, Weaviate; **pgvector (2021, by Andrew Kane)** added vector types + ANN indexing (**IVFFlat, HNSW**) to PostgreSQL. [Cognistik; Harbor Software; data-dynamics comparison; DeepWiki: pgvector index methods]
- Driven by **embeddings + AI**: semantic search, recommendations, and RAG pipelines store/query embeddings; vector DBs implement **approximate nearest neighbor (ANN)** algorithms instead of exact-match lookup. [Wikipedia: Vector database; arXiv]

### 5.3 Convergence trend
- Hybridization is the current direction: relational + vector (pgvector), relational + graph + key-value + search (modern Spanner), ANN indexes inside established engines — blurring era boundaries.

### Sources (Tier 1–4)
- arXiv "The Faiss Library" (tier 3), Neo4j/Wikipedia (tier 2), vendor engineering blogs + DeepWiki (tier 2–4).
- **Confidence: High for lineage/timeline; Moderate for market-share claims (not assessed).**

---

## Cross-Era Summary Table

| Era | Timeframe | Data model | Access paradigm | Landmark systems | Key trade-off |
|---|---|---|---|---|---|
| Hierarchical/Network | 1960s | Trees / graphs | Navigational (pointers) | IBM IMS, CODASYL DBTG | Tight coupling to physical storage |
| Relational | 1970s–1990s | Tables (relations) | Declarative (SQL) | System R, Oracle, DB2, PostgreSQL, MySQL | Logical/physical separation; vertical scale limits |
| NoSQL | 2000s–2010s | KV / doc / wide-column | API / query languages | Bigtable, Dynamo, Cassandra, MongoDB, Redis | Scale/schema flexibility vs. ACID/consistency |
| NewSQL | 2011– | Relational, distributed | SQL + strong consistency | H-Store, VoltDB, Spanner, F1, TiDB, CockroachDB | SQL/ACID at global scale (complexity) |
| Graph | 2000s– | Property graph / RDF | Pattern traversal (Cypher, Gremlin, SPARQL) | Neo4j, TinkerPop, triple stores | Deep traversal vs. analytical/general workloads |
| Vector | 2017– | Embeddings in vector space | ANN similarity search | FAISS, Milvus, Pinecone, Qdrant, pgvector | Semantic recall vs. exactness; index/memory costs |

---

## Key Findings
1. **Each era responds to the previous era's constraint**: navigational complexity → relational abstraction; relational vertical scaling limits → NoSQL horizontal scaling; NoSQL consistency gaps → NewSQL (SQL + scale); semantic/connected-data needs → graph; ML/AI embedding workloads → vector.
2. **The relational model's logical/physical separation (Codd, 1970) is the enduring architectural insight** that all later eras build on or react against.
3. **Terminology origins are well-attested**: "NoSQL" (Strozzi 1998 → Oskarsson/Evans 2009); "NewSQL" (Aslett, 451 Group, 2011).
4. **Convergence is the current trend** — modern systems increasingly combine models (Spanner: relational+graph+KV+search; pgvector: relational+vector).

## Confidence & Limitations
- **High/Confirmed** for eras 1–4 (multiple independent, primary-adjacent sources).
- **High/Moderate** for graph & vector (timelines verified; no market-share claims made).
- **Assumption:** report scope is historical/technical; competitive performance comparisons were not part of the brief.
- **Unknowns:** no primary-source verification of Neo4j's exact founding date beyond secondary sources; minor date discrepancies in secondary sources (e.g., Cassandra launch year) were resolved to 2008 per multiple sources.

## Sources (consolidated)
- IBM History: IMS; Edgar Codd; relational database — ibm.com/history
- Wikipedia: IBM IMS; CODASYL; Edgar F. Codd; IBM System R; NoSQL; VoltDB; Neo4j; Cypher (query language); Vector database
- Enginerds: A Brief History of Database Systems (1960s–today)
- Substack bytesbtrees: IMS, CODASYL & CODD's relational model
- Martin Fowler: NosqlDefinition
- Google Research: Spanner (OSDI 2012); F1: A Distributed SQL Database That Scales; Spanner Cloud docs
- USENIX OSDI 2012 Spanner paper (PDF); ACM DL record
- ACM SIGMOD Record: "What's Really New with NewSQL?" (2016)
- HandWiki/Wikiwand: NewSQL
- IT History: MongoDB 1.0 (2009)
- Meegle/dev.to/Eduzan: History of NoSQL
- arXiv 2401.08281: "The Faiss Library" (J. Johnson et al., 2024 revision; original 2017)
- SwiftTools blog: Vector Database Landscape; bigdataclouds.org: Evolution of Vector DBs
- Neo4j blog: RDF vs property graphs; Talisman substack; adhdecode Neo4j vs RDF
- DeepWiki: pgvector index methods; cognistik/harborsoftware/data-dynamics vector DB comparisons