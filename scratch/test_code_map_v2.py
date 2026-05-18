import sys
import os
from pathlib import Path

sys.path.append(os.getcwd())

from agent_cascade.tools.custom.code_map import CodeMap

tool = CodeMap()
class MockPool:
    class MockOps:
        base_dir = Path(os.getcwd())
    operation_manager = MockOps()

tool.agent_pool = MockPool()

# Mock HTML content
html_content = """
<!DOCTYPE html>
<html>
<head>
    <title>Test</title>
</head>
<body>
    <h1 id="main-title">Hello World</h1>
    <div class="container">
        <p class="text">Some content</p>
        <button id="submit-btn">Submit</button>
    </div>
    <script>
        function test() { console.log('hi'); }
    </script>
</body>
</html>
"""

# Mock CSS content
css_content = """
body { margin: 0; }
.container { padding: 20px; }
#main-title { color: red; }
@media (max-width: 600px) {
    .container { padding: 10px; }
}
"""

with open("scratch/test.html", "w") as f: f.write(html_content)
with open("scratch/test.css", "w") as f: f.write(css_content)

print("--- HTML Map ---")
print(tool.call('{"path": "scratch/test.html"}'))

print("\n--- CSS Map ---")
print(tool.call('{"path": "scratch/test.css"}'))

os.remove("scratch/test.html")
os.remove("scratch/test.css")
