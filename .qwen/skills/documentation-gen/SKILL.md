---
name: documentation-gen
description: Generate docstrings (Google/NumPy/Sphinx style), README files, and API documentation from code including best practices for clear documentation
source: manual
version: "1.0.0"
triggers:
  - "docstring"
  - "documentation"
  - "README"
  - "API docs"
  - "generate docs"
  - "sphinx"
  - "numpy style"
---

## Goal

Generate clear, consistent documentation from code including docstrings in Google/NumPy/Sphinx styles, README files with project context, and API reference documentation. Ensure all public interfaces are well-documented.

## Procedure

### Step 1 — Docstring style guide selection

**Google Style (recommended for most projects):**
```python
def calculate_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate the great-circle distance between two points on Earth.

    Uses the Haversine formula to compute distance in kilometers.

    Args:
        lat1: Latitude of point 1 in degrees (-90 to 90).
        lon1: Longitude of point 1 in degrees (-180 to 180).
        lat2: Latitude of point 2 in degrees (-90 to 90).
        lon2: Longitude of point 2 in degrees (-180 to 180).

    Returns:
        Distance between the two points in kilometers.

    Raises:
        ValueError: If latitude is outside [-90, 90] or longitude is outside [-180, 180].

    Example:
        >>> calculate_distance(40.7128, -74.0060, 51.5074, -0.1278)
        5570.23
    """
```

**NumPy Style (best for scientific/numerical code):**
```python
def normalize(values: list[float], axis: int = 0) -> np.ndarray:
    """Normalize values to zero mean and unit variance.

    Parameters
    ----------
    values : list of float
        Input data array or sequence.
    axis : int, optional, default=0
        Axis along which to normalize. Default is 0 (columns).

    Returns
    -------
    numpy.ndarray
        Normalized array with the same shape as input.

    See Also
    --------
    standardize : Similar function that doesn't clip outliers.

    Notes
    -----
    Uses Z-score normalization: z = (x - μ) / σ
    """
```

**Sphinx Style (best for projects using Sphinx documentation):**
```python
def create_client(base_url: str, api_key: str | None = None):
    """Create a new API client instance.

    :param base_url: The base URL of the API endpoint.
    :type base_url: str
    :param api_key: Optional API key for authentication. Defaults to env var ``API_KEY``.
    :type api_key: str or None
    :return: A configured client instance ready to make requests.
    :rtype: APIClient
    :raises ValueError: If ``base_url`` is empty or invalid.
    """
```

### Step 2 — Class docstring patterns

```python
class DataPipeline:
    """Orchestrate data processing through a configurable pipeline of stages.

    Each stage transforms the data and passes it to the next. Stages can be
    added, removed, or reordered at runtime. The pipeline supports parallel
    execution for independent branches.

    Attributes:
        stages: List of registered processing stages in execution order.
        max_workers: Maximum number of parallel workers for branch execution.
        debug_mode: If True, log detailed stage-by-stage progress.

    Example:
        >>> pipeline = DataPipeline(max_workers=4)
        >>> pipeline.add_stage(CleanDataStage())
        >>> pipeline.add_stage(TransformStage(config))
        >>> results = pipeline.run(raw_data)
    """

    def __init__(self, max_workers: int = 1, debug_mode: bool = False):
        """Initialize the pipeline with configuration.

        Args:
            max_workers: Number of parallel workers (default: 1 for sequential).
            debug_mode: Enable verbose logging of stage execution.
        """
```

### Step 3 — README generation template

**Standard project README structure:**

```markdown
# Project Name

Brief one-line description of what the project does.

## Features

- Feature 1 with brief explanation
- Feature 2 with brief explanation
- Feature 3 with brief explanation

## Quick Start

\`\`\`bash
pip install -r requirements.txt
python main.py
\`\`\`

## Installation

Detailed installation instructions for different environments:

### Prerequisites
- Python 3.10+
- [Other dependencies]

### Install from source
\`\`\`bash
git clone https://github.com/user/repo.git
cd repo
pip install -e .
\`\`\`

## Configuration

Key configuration options (environment variables, config files):

| Variable | Default | Description |
|---|---|---|
| `API_KEY` | *(required)* | Authentication key for the API |
| `LOG_LEVEL` | `INFO` | Logging verbosity: DEBUG, INFO, WARNING, ERROR |
| `MAX_WORKERS` | `4` | Number of concurrent workers |

## Usage Examples

\`\`\`python
from myproject import Client

client = Client(api_key="your-key")
result = client.fetch_data(query="search term")
print(result.items)
\`\`\`

## Project Structure

\`\`\`
myproject/
├── src/myproject/      # Main source code
│   ├── core.py         # Core functionality
│   └── utils.py        # Helper utilities
├── tests/              # Test suite
├── docs/               # Documentation sources
└── scripts/            # Utility and deployment scripts
\`\`\`

## Testing

\`\`\`bash
pytest tests/ -v --cov=src/myproject
\`\`\`

## License

MIT License — see [LICENSE](LICENSE) for details.
```

### Step 4 — API documentation from code

**Auto-generate API reference with docstring extraction:**

```python
import inspect, textwrap

def extract_api_docs(module) -> str:
    """Extract function signatures and docstrings from a module."""
    docs = []
    for name, obj in inspect.getmembers(module):
        if not name.startswith("_") and callable(obj):
            sig = inspect.signature(obj)
            params = list(sig.parameters.keys())
            first_line = (obj.__doc__ or "").strip().split("\n")[0]
            docs.append(f"### `{name}{sig}`\n\n{first_line}")
    return "\n\n---\n\n".join(docs)

# Usage:
# import mymodule
# print(extract_api_docs(mymodule))
```

**Document type signatures clearly:**
```python
def process_records(
    records: list[dict[str, str | int]],
    filter_keys: set[str] | None = None,
    sort_by: str = "timestamp",
    descending: bool = True,
) -> tuple[list[dict], dict[str, int]]:
    """Process and filter a batch of records.

    Args:
        records: List of record dictionaries with string or integer values.
        filter_keys: Optional set of keys to retain; None keeps all keys.
        sort_by: Field name to sort by (default: "timestamp").
        descending: Sort in descending order if True (default).

    Returns:
        A tuple of (filtered_records, summary_counts) where summary_counts
        maps each record type to its occurrence count.
    """
```

### Step 5 — Documentation best practices checklist

| Practice | Why | Example |
|---|---|---|
| Document the "why", not just the "what" | Code shows what; docs explain reasoning | `"# Sort by date (newest first) for display order"` |
| Include examples for non-obvious APIs | Reduces support questions and onboarding time | `Example: >>> func(1, 2)` |
| Document default values explicitly | Callers need to know what happens without args | `timeout: Seconds to wait (default: 30).` |
| Note side effects clearly | Functions that mutate state or have I/O should say so | `"Modifies the input list in-place."` |
| Keep docstrings in sync with code | Outdated docs are worse than no docs | Update docstring when signature changes |

### Step 6 — Sphinx setup (for full documentation sites)

**Minimal `conf.py`:**
```python
extensions = [
    "sphinx.ext.autodoc",      # Auto-generate from docstrings
    "sphinx.ext.napoleon",     # Google/NumPy style support
    "sphinx.ext.viewcode",     # Link to source code
]

project = "MyProject"
version = "1.0.0"
```

**Build and serve:**
```bash
# Generate HTML documentation
cd docs && make html

# Serve locally for review
python -m http.server --directory docs/_build/html 8000
```

## Key Configuration Values

| Parameter | Recommended | Why |
|---|---|---|
| Docstring style | Google (default), NumPy (scientific) | Most readable and widely adopted |
| Documentation coverage | All public APIs (`def`, `class`) | Private helpers can skip if self-documenting |
| Example requirement | At least one example per function | Concrete examples accelerate understanding |

## What NOT to do

- Do not write docstrings that merely repeat the parameter names — add meaningful descriptions
- Do not document implementation details in public API docs — document behavior and contracts
- Do not leave "TODO" or placeholder docstrings (`"""..."""`) — either write proper docs or remove them
- Do not use Sphinx if you don't need a full documentation site — simple README + inline docstrings suffice for small projects
- Do not skip documenting exceptions — callers need to know what can go wrong