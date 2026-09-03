"""API reference pages themed to match the PaperTick web app.

Swagger UI 5 ships a real dark mode (`html.dark-mode`), so that is switched on
rather than fighting the light theme with overrides — it is what makes the
schema/model panes readable. Only a thin palette sheet is layered on top to
move Swagger's neutral greys onto the app's slate surfaces and emerald accent.
Nothing but colors is touched, so a Swagger upgrade degrades to "that panel is
the wrong shade of grey", never to a broken page.

ReDoc has no dark mode of its own, so it is booted through `Redoc.init` with an
explicit theme object built from the same palette.

Where a foreground had to be forced, it is because the contrast was measured
and failed, not because a shade looked off: Swagger's expand arrows ship a
literal `fill="#000"` in the symbol markup (1.04:1 on slate), its prose is left
at the light theme's `#3b4151` (1.98:1), and both libraries put white text on
pale method badges (1.6-2.5:1). Each of those has a comment saying which.
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

# HTTP method colors, moved onto the app's own palette. Swagger's defaults are
# pale pastels carrying white text — around 1.6:1, which is what makes the
# method pills hard to read. These are the app's emerald / sky / amber / red,
# and every one of them is light enough to carry **black** text at 6:1 or
# better, so INK_ON_COLOR is used for anything sitting on a filled surface.
INK_ON_COLOR = "#04140d"
M_GET = "#38bdf8"     # sky-400
M_POST = "#10b981"    # emerald-500
M_PUT = "#f59e0b"     # amber-500
M_PATCH = "#2dd4bf"    # teal-400
M_DELETE = "#ef4444"  # red-500
M_HEAD = "#c084fc"    # purple-400
M_OPTIONS = "#94a3b8"  # slate-400

# The same methods again, several steps darker, for ReDoc.
#
# Swagger's method pill is a class we can style, so there it gets the bright
# color above and black text. ReDoc draws its verb badge with white text
# hard-coded in a styled-component, and its class names are emotion hashes that
# change between builds — so the only durable lever is the one color ReDoc does
# expose. These are chosen to carry *white* at 5.5:1 or better rather than to
# match Swagger's swatch exactly: a badge that is readable in both places beats
# two badges that are the same shade and one of them unreadable.
R_GET = "#0369a1"      # sky-700
R_POST = "#047857"     # emerald-700
R_PUT = "#b45309"      # amber-700
R_PATCH = "#0f766e"    # teal-700
R_DELETE = "#b91c1c"   # red-700
R_HEAD = "#7e22ce"     # purple-700
R_OPTIONS = "#475569"  # slate-600

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

/* ---- readability ----------------------------------------------------------
   Everything below fixes a measured contrast failure, not a preference.
   Swagger's dark mode recolors its surfaces but leaves several foregrounds at
   the light theme's values, so they end up near-black on near-black. */

/* The expand/collapse chevrons. Swagger draws them as an <svg class="arrow">
   pointing at a <symbol>, and the symbol carries `fill="#000"` in the markup —
   a stylesheet that only sets colors on text never touches it, so every arrow
   on the page was pure black on slate at 1.04:1, i.e. invisible. Fill has to
   be forced on the <use> as well as the <svg>, since the symbol's own
   attribute wins over an inherited value. */
html.dark-mode .swagger-ui svg.arrow,
html.dark-mode .swagger-ui svg.arrow use,
html.dark-mode .swagger-ui .expand-operation svg,
html.dark-mode .swagger-ui .expand-operation svg use,
html.dark-mode .swagger-ui .models-control svg,
html.dark-mode .swagger-ui .model-box-control svg,
html.dark-mode .swagger-ui .opblock-control-arrow svg {{
  fill: {TEXT} !important; color: {TEXT};
}}
html.dark-mode .swagger-ui .opblock-summary-control:hover svg.arrow use,
html.dark-mode .swagger-ui .opblock-tag:hover svg.arrow use {{ fill: #ffffff !important; }}
/* the authorize padlock keeps the accent, but has to be visible too */
html.dark-mode .swagger-ui .authorization__btn svg,
html.dark-mode .swagger-ui .authorization__btn svg use {{ fill: {ACCENT} !important; }}

/* Method pills: black on the app's colors, not white on Swagger's pastels. */
html.dark-mode .swagger-ui .opblock .opblock-summary-method {{
  color: {INK_ON_COLOR}; font-weight: 700; text-shadow: none; border-radius: 6px;
}}
html.dark-mode .swagger-ui .opblock.opblock-get .opblock-summary-method {{ background: {M_GET}; }}
html.dark-mode .swagger-ui .opblock.opblock-post .opblock-summary-method {{ background: {M_POST}; }}
html.dark-mode .swagger-ui .opblock.opblock-put .opblock-summary-method {{ background: {M_PUT}; }}
html.dark-mode .swagger-ui .opblock.opblock-patch .opblock-summary-method {{ background: {M_PATCH}; }}
html.dark-mode .swagger-ui .opblock.opblock-delete .opblock-summary-method {{ background: {M_DELETE}; }}
html.dark-mode .swagger-ui .opblock.opblock-head .opblock-summary-method {{ background: {M_HEAD}; }}
html.dark-mode .swagger-ui .opblock.opblock-options .opblock-summary-method {{ background: {M_OPTIONS}; }}

/* The operation row itself: one faint tint per method instead of Swagger's
   saturated wash, so the path and summary keep their contrast. */
html.dark-mode .swagger-ui .opblock {{ background: {CARD}; border-color: {BORDER}; }}
html.dark-mode .swagger-ui .opblock .opblock-summary {{ border-color: {BORDER}; }}
html.dark-mode .swagger-ui .opblock.opblock-get {{ border-color: rgba(56, 189, 248, .45); }}
html.dark-mode .swagger-ui .opblock.opblock-post {{ border-color: rgba(16, 185, 129, .45); }}
html.dark-mode .swagger-ui .opblock.opblock-put {{ border-color: rgba(245, 158, 11, .45); }}
html.dark-mode .swagger-ui .opblock.opblock-patch {{ border-color: rgba(45, 212, 191, .45); }}
html.dark-mode .swagger-ui .opblock.opblock-delete {{ border-color: rgba(239, 68, 68, .45); }}

/* Path, summary and section headings were all left at Swagger's #3b4151 —
   1.98:1 against the page. */
html.dark-mode .swagger-ui .opblock .opblock-summary-path,
html.dark-mode .swagger-ui .opblock .opblock-summary-path__deprecated,
html.dark-mode .swagger-ui .opblock .opblock-summary-path a,
html.dark-mode .swagger-ui .opblock .opblock-summary-operation-id {{
  color: #f1f5f9; font-family: {MONO};
}}
html.dark-mode .swagger-ui .opblock .opblock-summary-description,
html.dark-mode .swagger-ui .opblock-tag small,
html.dark-mode .swagger-ui .opblock-tag small p {{ color: {MUTED}; }}
html.dark-mode .swagger-ui .opblock-section-header {{
  background: rgba(2, 6, 23, .55); border-color: {BORDER}; box-shadow: none;
}}
html.dark-mode .swagger-ui .opblock-section-header h4,
html.dark-mode .swagger-ui .opblock-section-header > label,
html.dark-mode .swagger-ui .opblock-section-header > label span,
html.dark-mode .swagger-ui .opblock .opblock-section-header h4 span,
html.dark-mode .swagger-ui .opblock-title_normal,
html.dark-mode .swagger-ui .opblock-description-wrapper h4,
html.dark-mode .swagger-ui .opblock-external-docs-wrapper h4 {{ color: #f1f5f9; }}

/* Body copy: the API description, every operation's prose, and tables. */
html.dark-mode .swagger-ui .markdown p, html.dark-mode .swagger-ui .markdown li,
html.dark-mode .swagger-ui .markdown h1, html.dark-mode .swagger-ui .markdown h2,
html.dark-mode .swagger-ui .markdown h3, html.dark-mode .swagger-ui .markdown h4,
html.dark-mode .swagger-ui .renderedMarkdown p, html.dark-mode .swagger-ui .renderedMarkdown li,
html.dark-mode .swagger-ui .opblock-description-wrapper p,
html.dark-mode .swagger-ui .opblock-external-docs-wrapper p,
html.dark-mode .swagger-ui .response-col_description,
html.dark-mode .swagger-ui .response-col_description p,
html.dark-mode .swagger-ui .info li, html.dark-mode .swagger-ui .info p,
html.dark-mode .swagger-ui .info .description p,
html.dark-mode .swagger-ui .info .base-url,
html.dark-mode .swagger-ui .parameter__name,
html.dark-mode .swagger-ui table tbody tr td {{ color: {TEXT}; }}
html.dark-mode .swagger-ui .parameter__name.required span {{ color: #f87171; }}
html.dark-mode .swagger-ui .parameter__type,
html.dark-mode .swagger-ui .parameter__in,
html.dark-mode .swagger-ui .parameter__deprecated,
html.dark-mode .swagger-ui .response-col_status,
html.dark-mode .swagger-ui .response-col_links {{ color: {MUTED}; }}
html.dark-mode .swagger-ui .response-col_status {{ font-family: {MONO}; }}

/* Inline code: amber on a translucent slate measured 1.54:1. */
html.dark-mode .swagger-ui .markdown code, html.dark-mode .swagger-ui .renderedMarkdown code,
html.dark-mode .swagger-ui .info code, html.dark-mode .swagger-ui .info .description code {{
  background: rgba(2, 6, 23, .85); color: {CODE}; border: 1px solid {BORDER};
  padding: 1px 5px; border-radius: 5px; font-family: {MONO};
}}
/* Swagger sets the prose line-height from its own theme; the description block
   ran its lines into each other once code spans raised their height. */
html.dark-mode .swagger-ui .info .description p,
html.dark-mode .swagger-ui .markdown p, html.dark-mode .swagger-ui .renderedMarkdown p {{
  line-height: 1.7;
}}

/* Controls that sit on a filled surface take the same black text. */
html.dark-mode .swagger-ui .opblock .btn.execute,
html.dark-mode .swagger-ui .btn.execute {{ color: {INK_ON_COLOR}; font-weight: 600; }}
html.dark-mode .swagger-ui .info .title small.version-stamp,
html.dark-mode .swagger-ui .info .title small.version-stamp pre {{
  background: {ACCENT}; color: {INK_ON_COLOR};
}}
html.dark-mode .swagger-ui .info .title small pre {{ color: {TEXT}; }}
html.dark-mode .swagger-ui .response-control-media-type--accept-controller select {{
  border-color: {ACCENT};
}}
html.dark-mode .swagger-ui .response-control-media-type__accept-message {{ color: {ACCENT}; }}
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

/* The media-type heading above each request/response sample
   ("application/json", "text/plain") renders as pure black — 1.04:1 against
   the panel behind it, so the label is simply not there. It is a plain <h5>,
   which is stable enough to target even though its class is an emotion hash. */
#redoc h5, #redoc h5 span {{ color: {TEXT} !important; }}

/* ReDoc paints the *selected* sample tab using the theme's primary **text**
   colour as its background, then leaves the label that same colour — light on
   light, measured at 1.0:1, i.e. the selected tab's text vanishes exactly when
   it is the one you are reading. The class names here come from the react-tabs
   library and ReDoc's own status modifiers, not from emotion, so they hold. */
#redoc .react-tabs__tab {{ color: {MUTED}; background: transparent; border-radius: 6px 6px 0 0; }}
#redoc .react-tabs__tab--selected {{ background: {RAISED} !important; color: {TEXT} !important; }}
#redoc .react-tabs__tab--selected.tab-success {{ color: {ACCENT} !important; }}
#redoc .react-tabs__tab--selected.tab-error {{ color: #f87171 !important; }}
#redoc .react-tabs__tab--selected.tab-redirect {{ color: {CODE} !important; }}
#redoc .react-tabs__tab--selected.tab-info {{ color: {TYPE} !important; }}

/* Response accordions carry a translucent status tint; the heading on top of
   it was still the muted body colour. */
#redoc h3, #redoc h3 span, #redoc button h3 {{ color: #f8fafc; }}
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
      http: { get: "__R_GET__", post: "__R_POST__", put: "__R_PUT__",
              options: "__R_OPTIONS__", patch: "__R_PATCH__", delete: "__R_DELETE__",
              basic: "__MUTED__", link: "__TYPE__", head: "__R_HEAD__" }
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
# Relative to /api/, which is where both pages live: at a domain root the
# browser resolves them to /api/docs-assets/..., and behind a sub-folder proxy
# (BASE_PATH) to /papertick/api/docs-assets/... — no prefix to thread through.
SWAGGER_JS = "docs-assets/swagger-ui-bundle.js"
SWAGGER_CSS_URL = "docs-assets/swagger-ui.css"
REDOC_JS = "docs-assets/redoc.standalone.js"

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
    # What the pages *reference* — relative, so it resolves under a sub-folder
    # deployment too. What the route is *mounted* at stays absolute.
    spec_href = "openapi.json"
    if not public:
        @app.get(spec_url, include_in_schema=False, dependencies=guard)
        def openapi_spec() -> JSONResponse:
            return JSONResponse(app.openapi())

    @app.get("/api/docs", include_in_schema=False, dependencies=guard)
    def swagger_ui() -> HTMLResponse:
        html = get_swagger_ui_html(
            openapi_url=spec_href,
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
            "<body>", f"<body>{_chrome(app_url, ('redoc', 'ReDoc'))}", 1
        )
        return _docs_response(body)

    @app.get("/api/redoc", include_in_schema=False, dependencies=guard)
    def redoc_ui() -> HTMLResponse:
        html = REDOC_HTML
        for key, value in {
            "__TITLE__": f"{app.title} — reference",
            "__SPEC__": spec_href,
            "__CSS__": REDOC_CSS,
            "__REDOC_JS__": REDOC_JS,
            "__BAR__": _chrome(app_url, ("docs", "Swagger UI")),
            "__BG__": BG, "__CARD__": CARD, "__BORDER__": BORDER, "__TEXT__": TEXT,
            "__MUTED__": MUTED, "__ACCENT__": ACCENT, "__CODE__": CODE, "__TYPE__": TYPE,
            "__RAISED__": RAISED,
            "__R_GET__": R_GET, "__R_POST__": R_POST, "__R_PUT__": R_PUT,
            "__R_PATCH__": R_PATCH, "__R_DELETE__": R_DELETE,
            "__R_HEAD__": R_HEAD, "__R_OPTIONS__": R_OPTIONS,
            "__FONT__": FONT, "__MONO__": MONO,
        }.items():
            html = html.replace(key, value)
        return _docs_response(html)
