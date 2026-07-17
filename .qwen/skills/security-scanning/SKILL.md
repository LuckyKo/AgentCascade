---
name: security-scanning
description: Security audit patterns including dependency checking, secret detection, common vulnerability patterns (SQL injection, XSS), and input validation
source: manual
version: "1.0.0"
triggers:
  - "security scan"
  - "vulnerability"
  - "secret detection"
  - "dependency check"
  - "sql injection"
  - "xss"
  - "input validation"
---

## Goal

Audit code and dependencies for common security vulnerabilities, detect hardcoded secrets, validate inputs against injection attacks, and ensure dependency versions are up to date.

## Procedure

### Step 1 — Dependency checking

**Find outdated packages:**
```bash
# List all installed packages with newer versions available
pip list --outdated

# Format as table for readability
pip list --outdated --format=columns

# Check for known vulnerabilities in dependencies
pip install pip-audit
pip-audit

# Or using pip-tools to pin and update
pip-compile requirements.in -o requirements.txt
pip-sync requirements.txt
```

**Check transitive dependency tree:**
```bash
# Show full dependency tree with versions
pip install pipdeptree
pipdeptree --warn-failed

# Check for version conflicts
pip check
```

### Step 2 — Secret detection

**Common secrets to look for:**
```python
import re

SECRET_PATTERNS = {
    "api_key": r'(?:API[_\s]?KEY|api[_\s]?key)\s*[:=]\s*["\']?([A-Za-z0-9_\-]{16,})',
    "password": r'(?:PASSWORD|PASSWD|pass)\s*[:=]\s*["\']?(\S+)',
    "token": r'(?:TOKEN|Bearer)\s*[:=]\s*["\']?([A-Za-z0-9_\-\.]{20,})',
    "aws_key": r'AKIA[0-9A-Z]{16}',
    "email_secret": r'[0-9a-fA-F]{40}@[0-9a-fA-F]{32}\.xx',
}

def find_secrets_in_file(filepath: str) -> list[dict]:
    """Scan a file for potential hardcoded secrets."""
    findings = []
    with open(filepath) as f:
        for line_no, line in enumerate(f, 1):
            for name, pattern in SECRET_PATTERNS.items():
                matches = re.finditer(pattern, line, re.IGNORECASE)
                for match in matches:
                    findings.append({
                        "file": filepath,
                        "line": line_no,
                        "type": name,
                        "value": match.group(1)[:8] + "...",  # Partial reveal
                    })
    return findings

# Quick scan of Python files
import glob
for f in glob.glob("src/**/*.py", recursive=True):
    secrets = find_secrets_in_file(f)
    if secrets:
        for s in secrets:
            print(f"⚠️ {s['file']}:{s['line']} — {s['type']}: {s['value']}")
```

**Best practice: Use environment variables or config files:**
```python
import os
from pathlib import Path

# Option 1: Environment variable with fallback
API_KEY = os.getenv("API_KEY", "")

# Option 2: .env file (with python-dotenv)
from dotenv import load_dotenv
load_dotenv()
DB_PASSWORD = os.getenv("DB_PASSWORD")

# Option 3: Config file (YAML/JSON)
import yaml
config = yaml.safe_load(Path("config/secrets.yaml").read_text())
```

### Step 3 — Common vulnerability patterns

**SQL Injection:**
```python
# ❌ Vulnerable: string concatenation
query = f"SELECT * FROM users WHERE id = {user_id}"
query = "SELECT * FROM users WHERE name = '" + name + "'"

# ✅ Safe: parameterized queries
cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))

# ✅ Safe: ORM approach
users = User.query.filter_by(id=user_id).all()

# Also check for injection in JOIN conditions and ORDER BY clauses
order_column = "name"  # Validate against whitelist!
ALLOWED_ORDER_COLUMNS = {"id", "name", "created_at"}
```

**XSS (Cross-Site Scripting):**
```python
# ❌ Vulnerable: unescaped output
html = f"<div>{user_input}</div>"

# ✅ Safe: escape HTML entities
from html import escape
html = f"<div>{escape(user_input)}</div>"

# For Jinja2 templates, escaping is automatic — just avoid {{ variable }} in attributes without quotes
```

**Common vulnerability checklist:**

| Vulnerability | What to check for | Fix pattern |
|---|---|---|
| SQL Injection | `f"SELECT..."`, string concat queries | Parameterized queries or ORM |
| XSS | Unescaped HTML output, `<script>` tags | `escape()` or template auto-escaping |
| Hardcoded secrets | API keys in source files | Environment variables or `.env` |
| Missing auth headers | No token on outgoing requests | Add `Authorization: Bearer <token>` |
| Insecure defaults | `debug=True`, `verify=False` | Set explicitly for production |
| Buffer overflow (Python) | Reading entire file into memory | Use streaming/chunked reads |

### Step 4 — Input validation patterns

```python
from pydantic import BaseModel, Field, field_validator

class UserInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(pattern=r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")
    age: int = Field(ge=0, le=150)
    
    @field_validator("name")
    @classmethod
    def name_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Name cannot be whitespace only")
        return v.strip()

# Validate incoming data
try:
    user = UserInput(name=" Alice ", email="alice@example.com", age=30)
except Exception as e:
    print(f"Validation error: {e}")
```

**Simple validation without Pydantic:**
```python
def validate_input(data: dict, schema: dict) -> list[str]:
    """Validate data against a simple schema definition."""
    errors = []
    for key, rules in schema.items():
        value = data.get(key)
        if rules.get("required") and value is None:
            errors.append(f"Missing required field: {key}")
        elif value is not None:
            if "type" in rules and not isinstance(value, rules["type"]):
                errors.append(f"{key}: expected {rules['type'].__name__}, got {type(value).__name__}")
            if "min_length" in rules and len(str(value)) < rules["min_length"]:
                errors.append(f"{key}: too short (min {rules['min_length']})")
    return errors

schema = {
    "name": {"required": True, "type": str, "min_length": 1},
    "email": {"required": True, "type": str},
    "age": {"required": False, "type": int},
}
errors = validate_input({"name": "", "email": "test@example.com"}, schema)
```

### Step 5 — Quick security audit script

```bash
#!/bin/bash
echo "=== Security Audit ==="

# Check for outdated dependencies
echo -e "\n📦 Outdated packages:"
pip list --outdated | head -20

# Scan for hardcoded secrets
echo -e "\n🔑 Potential secrets in source:"
grep -rn "API_KEY\|PASSWORD\|SECRET\|TOKEN" src/ \
  --include="*.py" | grep -v "^Binary" | head -15

# Check for debug flags
echo -e "\n🐛 Debug mode checks:"
grep -rn "debug\s*=\s*True\|DEBUG\s*=" src/ --include="*.py"

# Check for wildcard imports
echo -e "\n📥 Wildcard imports:"
grep -rn "^from .* import \*" src/ --include="*.py"

# Check for unused variables (potential dead code)
echo -e "\n✅ Audit complete. Review findings above."
```

## Key Configuration Values

| Parameter | Recommended | Why |
|---|---|---|
| Secret scan frequency | Every commit / pre-push hook | Catches secrets before they're pushed |
| Dependency update cadence | Weekly minor, monthly major | Balances freshness against breakage risk |
| Validation strictness | Strict on all external inputs | Defense in depth — trust nothing from outside |

## What NOT to do

- Do not hardcode credentials in source files — even for "temporary" testing
- Do not skip input validation because "it's internal only" — internal callers can change too
- Do not use `pip list --outdated` as the only check — also run `pip-audit` for CVEs
- Do not concatenate strings to build SQL queries — always parameterize
- Do not ignore security warnings in CI — treat them as failures, not notices