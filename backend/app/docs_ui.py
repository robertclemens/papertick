"""API reference pages themed to match the PaperTick web app.

Swagger UI 5 ships a real dark mode (`html.dark-mode`), so that is switched on
rather than fighting the light theme with overrides — it is what makes the
schema/model panes readable. Only a thin palette sheet is layered on top to
move Swagger's neutral greys onto the app's slate surfaces and emerald accent.
Nothing but colors is touched, so a Swagger upgrade degrades to "that panel is
the wrong shade of grey", never to a broken page.

ReDoc has no dark mode of its own, so it is booted through `Redoc.init` with an
explicit theme object built from the same palette.
"""

from fastapi import FastAPI
from fastapi.openapi.docs import get_swagger_ui_html
from pathlib import Path

from fastapi import Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# frontend/app/globals.css
BG = "#020617"        # slate-950, page
CARD = "#0f172a"      # slate-900, raised surfaces
RAISED = "#1e293b"    # slate-800, hover / chips
BORDER = "#243449"
TEXT = "#e2e8f0"
MUTED = "#94a3b8"
ACCENT = "#10b981"    # emerald-500
CODE = "#fbbf24"
TYPE = "#7dd3fc"

FONT = 'ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif'
MONO = 'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace'

SWAGGER_CSS = f"""
html.dark-mode, html.dark-mode body, html.dark-mode .swagger-ui {{
  background: {BG}; color: {TEXT};
}}
html.dark-mode .swagger-ui, html.dark-mode .swagger-ui .info .title,
html.dark-mode .swagger-ui .opblock-tag, html.dark-mode .swagger-ui .btn {{ font-family: {FONT}; }}
html.dark-mode .swagger-ui .info .title {{ color: #f8fafc; }}
html.dark-mode .swagger-ui a, html.dark-mode .swagger-ui .info a {{ color: {ACCENT}; }}
html.dark-mode .swagger-ui .info .title small.version-stamp {{ background: #065f46; }}
html.dark-mode .swagger-ui .info .title small {{ background: {RAISED}; }}
html.dark-mode .swagger-ui .topbar {{ display: none; }}
html.dark-mode .swagger-ui .scheme-container {{
  background: {CARD}; box-shadow: none; border-bottom: 1px solid {BORDER};
}}
html.dark-mode .swagger-ui .opblock-tag {{ border-bottom: 1px solid {BORDER}; color: #f1f5f9; }}
html.dark-mode .swagger-ui .opblock {{ border-radius: 12px; box-shadow: none; }}
html.dark-mode .swagger-ui .btn {{ border-color: {BORDER}; color: {TEXT}; border-radius: 8px; }}
html.dark-mode .swagger-ui .btn:hover {{ background: {RAISED}; }}
html.dark-mode .swagger-ui .opblock .btn.execute {{
  background: {ACCENT}; border-color: {ACCENT}; color: {BG};
}}
html.dark-mode .swagger-ui .btn.authorize {{ color: {ACCENT}; border-color: {ACCENT}; }}
html.dark-mode .swagger-ui .btn.authorize svg {{ fill: {ACCENT}; }}
html.dark-mode .swagger-ui input, html.dark-mode .swagger-ui textarea,
html.dark-mode .swagger-ui select {{
  background: {BG}; border-color: #334155; color: {TEXT}; border-radius: 8px;
}}
html.dark-mode .swagger-ui .dialog-ux .modal-ux {{
  background-color: {CARD}; border: 1px solid {BORDER};
}}

/* ---- schemas & models: the part that has to stay legible ---- */
html.dark-mode .swagger-ui section.models {{
  background: {CARD}; border: 1px solid {BORDER}; border-radius: 12px;
}}
html.dark-mode .swagger-ui section.models h4,
html.dark-mode .swagger-ui section.models.is-open h4 {{ border-color: {BORDER}; }}
html.dark-mode .swagger-ui section.models h4 span,
html.dark-mode .swagger-ui section.models h5 {{ color: #f1f5f9; }}
html.dark-mode .swagger-ui section.models h4:hover {{ background: rgba(148, 163, 184, .06); }}
html.dark-mode .swagger-ui section.models .model-container,
html.dark-mode .swagger-ui .model-box,
html.dark-mode .swagger-ui .json-schema-2020-12,
html.dark-mode .swagger-ui .json-schema-2020-12 button {{
  background: rgba(2, 6, 23, .6); border-radius: 10px;
}}
html.dark-mode .swagger-ui section.models .model-container:hover {{ background: rgba(2, 6, 23, .85); }}
html.dark-mode .swagger-ui .model-box .model, html.dark-mode .swagger-ui .model-box .model-title,
html.dark-mode .swagger-ui .model-title, html.dark-mode .swagger-ui .json-schema-2020-12__title {{
  color: #f1f5f9;
}}
html.dark-mode .swagger-ui .model, html.dark-mode .swagger-ui .model .property,
html.dark-mode .swagger-ui .model .property-row, html.dark-mode .swagger-ui table.model tr td,
html.dark-mode .swagger-ui .json-schema-2020-12-keyword__name--primary,
html.dark-mode .swagger-ui .json-schema-2020-12-json-viewer__name--primary {{ color: {TEXT}; }}
html.dark-mode .swagger-ui table.model tr td:first-of-type,
html.dark-mode .swagger-ui .json-schema-2020-12-property .json-schema-2020-12__title {{
  font-family: {MONO}; color: #f8fafc;
}}
html.dark-mode .swagger-ui .model .prop-type, html.dark-mode .swagger-ui .prop-type,
html.dark-mode .swagger-ui .json-schema-2020-12__attribute--primary,
html.dark-mode .swagger-ui .model-box-control:not(.prop) {{ color: {TYPE}; }}
html.dark-mode .swagger-ui .model .prop-format, html.dark-mode .swagger-ui .prop-format,
html.dark-mode .swagger-ui .model .property.primitive,
html.dark-mode .swagger-ui table.model tr.description,
html.dark-mode .swagger-ui .json-schema-2020-12-keyword--description,
html.dark-mode .swagger-ui .json-schema-2020-12-keyword__name--secondary,
html.dark-mode .swagger-ui .json-schema-2020-12-keyword__value--secondary,
html.dark-mode .swagger-ui .json-schema-2020-12__attribute--muted {{ color: {MUTED}; }}
html.dark-mode .swagger-ui .model .brace-open, html.dark-mode .swagger-ui .model .brace-close,
html.dark-mode .swagger-ui .model .braces {{ color: #64748b; }}
html.dark-mode .swagger-ui table.model tr.property-row .star,
html.dark-mode .swagger-ui .model .property-row.required .star,
html.dark-mode .swagger-ui .json-schema-2020-12-property--required
  > .json-schema-2020-12:first-of-type > .json-schema-2020-12-head .json-schema-2020-12__title:after {{
  color: #f87171;
}}
html.dark-mode .swagger-ui .json-schema-2020-12__constraint {{ background: {RAISED}; color: {TYPE}; }}
html.dark-mode .swagger-ui .json-schema-2020-12__constraint--string {{ background: #422006; color: {CODE}; }}
html.dark-mode .swagger-ui .json-schema-2020-12-body,
html.dark-mode .swagger-ui .json-schema-2020-12-keyword__children,
html.dark-mode .swagger-ui .json-schema-2020-12-json-viewer__children {{ border-color: #334155; }}
html.dark-mode .swagger-ui .model-hint {{ background: {RAISED}; color: {TEXT}; }}
html.dark-mode .swagger-ui .opblock pre.microlight,
html.dark-mode .swagger-ui .opblock .highlight-code pre.microlight {{
  background: {BG} !important; color: #cbd5e1; border: 1px solid {BORDER}; border-radius: 10px;
}}
html.dark-mode .swagger-ui .markdown code, html.dark-mode .swagger-ui .renderedMarkdown code {{
  background: rgba(148, 163, 184, .16); color: {CODE};
}}
html.dark-mode .swagger-ui .copy-to-clipboard, html.dark-mode .swagger-ui .download-contents {{
  background: {RAISED};
}}
html.dark-mode .swagger-ui .tab li button.tablinks {{ color: {MUTED}; }}
html.dark-mode .swagger-ui .tab li.active button.tablinks {{ color: {ACCENT}; }}
html.dark-mode .swagger-ui table thead tr td, html.dark-mode .swagger-ui table thead tr th {{
  border-color: {BORDER}; color: {MUTED};
}}
"""

CHROME_CSS = f"""
.pt-docs-bar {{
  display: flex; align-items: center; gap: 12px; padding: 14px 22px;
  background: rgba(15, 23, 42, .8); border-bottom: 1px solid {BORDER};
  font-family: {FONT}; position: sticky; top: 0; z-index: 20; backdrop-filter: blur(6px);
}}
.pt-docs-bar .pt-mark {{
  display: inline-flex; align-items: center; justify-content: center; height: 28px; width: 28px;
  border-radius: 8px; background: {ACCENT}; color: {BG}; font-weight: 700;
}}
.pt-docs-bar .pt-name {{ color: #f1f5f9; font-size: 17px; font-weight: 600; }}
.pt-docs-bar .pt-name em {{ color: {ACCENT}; font-style: normal; }}
.pt-docs-bar .pt-links {{ margin-left: auto; display: flex; gap: 8px; }}
.pt-docs-bar a {{
  color: {MUTED}; text-decoration: none; font-size: 14px; font-family: {FONT};
  border: 1px solid {BORDER}; border-radius: 8px; padding: 6px 12px;
}}
.pt-docs-bar a:hover {{ color: {TEXT}; background: {RAISED}; }}
"""


def _chrome(app_url: str, other: tuple[str, str]) -> str:
    return (
        '<div class="pt-docs-bar">'
        '<span class="pt-mark">P</span>'
        '<span class="pt-name">Paper<em>Tick</em> API</span>'
        '<span class="pt-links">'
        f'<a href="{other[0]}">{other[1]}</a>'
        f'<a href="{app_url}">&larr; Back to the app</a>'
        "</span></div>"
    )


REDOC_CSS = f"""
body {{ margin: 0; background: {BG}; color: {TEXT}; }}
/* ReDoc styles through emotion, so its generated class names are not stable.
   These target plain elements inside the mount point instead — enough to catch
   the controls ReDoc's theme object does not reach (search box, tables, the
   copy/expand buttons and any panel that defaults to a white surface). */
#redoc, #redoc * {{ scrollbar-color: {RAISED} {BG}; }}
#redoc input, #redoc select, #redoc textarea {{
  background: {BG} !important; color: {TEXT} !important;
  border: 1px solid {BORDER} !important; border-radius: 8px;
}}
#redoc input::placeholder {{ color: {MUTED} !important; }}
#redoc table th, #redoc table td {{ color: {TEXT}; border-color: {BORDER}; }}
#redoc button {{ color: {TEXT}; }}
#redoc h1, #redoc h2, #redoc h3, #redoc h4, #redoc h5 {{ color: #f8fafc; }}
#redoc a {{ color: {ACCENT}; }}
#redoc code, #redoc pre {{ background: {BG}; color: {CODE}; }}
#redoc svg {{ fill: currentColor; }}
{CHROME_CSS}
"""

REDOC_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>__TITLE__</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="shortcut icon" href="data:,">
<style>__CSS__</style>
</head>
<body>__BAR__<div id="redoc"></div>
<script src="__REDOC_JS__"></script>
<script>
if (!window.Redoc) {
  document.getElementById("redoc").innerHTML =
    '<p style="padding:24px;font-family:__FONT__;color:__MUTED__">' +
    'The ReDoc bundle could not be loaded (no network to the CDN). ' +
    '<a style="color:__ACCENT__" href="/api/docs">Use Swagger UI instead</a>.</p>';
} else {
Redoc.init("__SPEC__", {
  hideDownloadButton: false,
  expandResponses: "200,201",
  theme: {
    colors: {
      tonalOffset: 0.2,
      primary: { main: "__ACCENT__", contrastText: "__BG__" },
      success: { main: "__ACCENT__", contrastText: "__BG__" },
      warning: { main: "#c98500", contrastText: "__BG__" },
      error:   { main: "#d03b3b", contrastText: "__BG__" },
      // ReDoc paints several panels from the gray ramp; left light they are
      // the white boxes that made body text unreadable
      gray: { 50: "__CARD__", 100: "__RAISED__" },
      border: { dark: "__BORDER__", light: "__BORDER__" },
      text: { primary: "__TEXT__", secondary: "__MUTED__" },
      responses: {
        success:  { color: "__ACCENT__", backgroundColor: "rgba(16,185,129,.12)" },
        error:    { color: "#f87171",   backgroundColor: "rgba(208,59,59,.12)" },
        redirect: { color: "#fbbf24",   backgroundColor: "rgba(201,133,0,.12)" },
        info:     { color: "__TYPE__",  backgroundColor: "rgba(57,135,229,.12)" }
      },
      http: { get: "#3987e5", post: "__ACCENT__", put: "#c98500", options: "#7dd3fc",
              patch: "#d55181", delete: "#d03b3b", basic: "__MUTED__",
              link: "__TYPE__", head: "#b889ff" }
    },
    typography: {
      fontSize: "15px", lineHeight: "1.6",
      fontFamily: '__FONT__', smoothing: "antialiased",
      headings: { fontFamily: '__FONT__', fontWeight: "600" },
      code: {
        fontFamily: '__MONO__', fontSize: "13px", color: "__CODE__",
        backgroundColor: "__BG__", wrap: true
      },
      links: {
        color: "__ACCENT__", visited: "__ACCENT__", hover: "#34d399",
        textDecoration: "none", hoverTextDecoration: "underline"
      }
    },
    sidebar: {
      backgroundColor: "__CARD__", textColor: "__TEXT__", activeTextColor: "__ACCENT__",
      arrow: { color: "__MUTED__" },
      groupItems: { textTransform: "none", activeBackgroundColor: "__RAISED__",
                    activeTextColor: "__ACCENT__" },
      level1Items: { textTransform: "none", activeBackgroundColor: "__RAISED__",
                     activeTextColor: "__ACCENT__" }
    },
    // the dark request/response column: samples, the server URL box and the
    // endpoint dropdown all live here
    rightPanel: {
      backgroundColor: "__CARD__", textColor: "__TEXT__",
      servers: {
        overlay: { backgroundColor: "__BG__", textColor: "__TEXT__" },
        url: { backgroundColor: "__BG__" }
      }
    },
    codeBlock: { backgroundColor: "__BG__" },
    fab: { backgroundColor: "__RAISED__", color: "__TEXT__" },
    schema: {
      linesColor: "__BORDER__", nestedBackground: "__BG__",
      typeNameColor: "__TYPE__", typeTitleColor: "__TYPE__",
      requireLabelColor: "#f87171", labelsTextSize: "0.9em",
      arrow: { color: "__MUTED__" }
    }
  }
}, document.getElementById("redoc"));
}
</script></body></html>
"""


STATIC_DIR = Path(__file__).parent / "static"

# Swagger UI and ReDoc are served from app/static rather than a CDN: an
# unauthenticated page on the API's own origin must not execute third-party
# JavaScript pinned to a floating version with no integrity hash. See
# app/static/README.md.
SWAGGER_JS = "/api/docs-assets/swagger-ui-bundle.js"
SWAGGER_CSS_URL = "/api/docs-assets/swagger-ui.css"
REDOC_JS = "/api/docs-assets/redoc.standalone.js"

# The docs pages do render markup, so they cannot use the API's `default-src
# 'none'` policy — but everything they load now comes from this origin.
DOCS_CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; "
    "connect-src 'self'; worker-src 'self' blob:; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'self'; object-src 'none'"
)


def _docs_response(body: str) -> HTMLResponse:
    return HTMLResponse(body, headers={"Content-Security-Policy": DOCS_CSP})


def install(app: FastAPI, app_url: str, public: bool = True) -> None:
    """Mount themed /api/docs and /api/redoc (app.docs_url must be None).

    `public=False` (production) puts the pages and the spec behind the same
    read authorization as the rest of the API, so the full description of the
    attack surface is not handed to anonymous callers.
    """
    from app.deps import require_read

    guard: list = [] if public else [Depends(require_read)]

    app.mount("/api/docs-assets", StaticFiles(directory=STATIC_DIR), name="docs-assets")

    spec_url = "/api/openapi.json"
    if not public:
        @app.get(spec_url, include_in_schema=False, dependencies=guard)
        def openapi_spec() -> JSONResponse:
            return JSONResponse(app.openapi())

    @app.get("/api/docs", include_in_schema=False, dependencies=guard)
    def swagger_ui() -> HTMLResponse:
        html = get_swagger_ui_html(
            openapi_url=app.openapi_url or spec_url,
            title=f"{app.title} — reference",
            swagger_js_url=SWAGGER_JS,
            swagger_css_url=SWAGGER_CSS_URL,
            # Off deliberately: persisting the credential writes any API key
            # pasted into the Authorize dialog into localStorage on the API's
            # own origin, where any script on the page can read it.
            swagger_ui_parameters={"docExpansion": "none", "persistAuthorization": False},
        )
        body = html.body.decode()
        # Swagger UI 5's own dark theme; the sheet below only re-tints it
        body = body.replace("<html>", '<html class="dark-mode">', 1)
        body = body.replace("</head>", f"<style>{SWAGGER_CSS}{CHROME_CSS}</style></head>")
        body = body.replace(
            "<body>", f"<body>{_chrome(app_url, ('/api/redoc', 'ReDoc'))}", 1
        )
        return _docs_response(body)

    @app.get("/api/redoc", include_in_schema=False, dependencies=guard)
    def redoc_ui() -> HTMLResponse:
        html = REDOC_HTML
        for key, value in {
            "__TITLE__": f"{app.title} — reference",
            "__SPEC__": app.openapi_url or spec_url,
            "__CSS__": REDOC_CSS,
            "__REDOC_JS__": REDOC_JS,
            "__BAR__": _chrome(app_url, ("/api/docs", "Swagger UI")),
            "__BG__": BG, "__CARD__": CARD, "__BORDER__": BORDER, "__TEXT__": TEXT,
            "__MUTED__": MUTED, "__ACCENT__": ACCENT, "__CODE__": CODE, "__TYPE__": TYPE,
            "__RAISED__": RAISED,
            "__FONT__": FONT, "__MONO__": MONO,
        }.items():
            html = html.replace(key, value)
        return _docs_response(html)
