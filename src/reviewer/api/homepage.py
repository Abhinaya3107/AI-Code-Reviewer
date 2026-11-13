"""The page shown at the bare URL of a deployed instance."""

REPO_URL = "https://github.com/Abhinaya3107/AI-Code-Reviewer"

HOMEPAGE_HTML = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Code Reviewer</title>
<style>
  :root {{
    --bg: #f6f7f9; --card: #ffffff; --text: #1b1f24; --muted: #5b6573; --border: #d9dde3;
    --accent: #2563eb; --accent-text: #ffffff; --ok: #1a9a4a;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #0f1216; --card: #171b21; --text: #e8ebef; --muted: #9aa4b2; --border: #2a313b;
      --accent: #4c8dff; --accent-text: #0b1220; --ok: #3ddc84;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 16px;
    background: var(--bg); color: var(--text);
    font: 16px/1.55 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  }}
  main {{
    width: 100%; max-width: 560px; padding: 32px 28px;
    background: var(--card); border: 1px solid var(--border); border-radius: 14px;
  }}
  h1 {{ margin: 0 0 8px; font-size: 1.75rem; line-height: 1.2; }}
  p {{ margin: 0 0 20px; color: var(--muted); }}
  .status {{
    display: inline-flex; align-items: center; gap: 8px; margin-bottom: 24px;
    padding: 6px 12px; border: 1px solid var(--border); border-radius: 999px; font-size: 0.9rem;
  }}
  .dot {{ width: 9px; height: 9px; border-radius: 50%; background: var(--ok); }}
  .links {{ display: flex; flex-wrap: wrap; gap: 10px; }}
  a.button {{
    padding: 9px 16px; border-radius: 8px; text-decoration: none; font-weight: 600;
    background: var(--accent); color: var(--accent-text);
  }}
  a.button.secondary {{ background: transparent; color: var(--text); border: 1px solid var(--border); }}
  a:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
  footer {{ margin-top: 20px; font-size: 0.85rem; }}
  footer a {{ color: var(--muted); }}
</style>
</head>
<body>
<main>
  <h1>AI Code Reviewer</h1>
  <p>
    A GitHub App that reviews pull requests with an LLM (Gemini). It reads the diff, finds likely bugs
    and risks, and posts a summary plus inline comments on the pull request.
  </p>
  <div class="status" role="status"><span class="dot" aria-hidden="true"></span>API status: Online</div>
  <div class="links">
    <a class="button" href="/docs">API docs (Swagger)</a>
    <a class="button secondary" href="{REPO_URL}">GitHub repository</a>
  </div>
  <footer><a href="/health">Health check</a></footer>
</main>
</body>
</html>
"""
