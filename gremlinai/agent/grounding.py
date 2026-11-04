"""
GremlinAI — DOM grounding.

Extracts interactive elements from the live page and returns:
- A numbered ElementRef list (capped at 60, in-viewport first)
- A short visible-text summary for the verifier
"""
from __future__ import annotations
import logging
from typing import TYPE_CHECKING

from playwright.async_api import Page

from gremlinai.models import ElementRef

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# JS injected into the page to collect element data
_GROUND_JS = """
() => {
    const SELECTORS = 'a,button,input,select,textarea,[role="button"],[role="link"],[role="checkbox"],[role="radio"],[role="combobox"],[role="menuitem"],[onclick],[tabindex]';
    const MAX = 80;

    function inViewport(el) {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0 &&
               r.top < window.innerHeight && r.bottom > 0 &&
               r.left < window.innerWidth && r.right > 0;
    }

    function isVisible(el) {
        if (el.getClientRects().length === 0) return false;
        const style = window.getComputedStyle(el);
        return style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0';
    }

    function accessibleName(el) {
        if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
        const labelledBy = el.getAttribute('aria-labelledby');
        if (labelledBy) {
            const lbl = document.getElementById(labelledBy);
            if (lbl) return lbl.innerText.trim();
        }
        if (el.id) {
            const lbl = document.querySelector('label[for="' + el.id + '"]');
            if (lbl) return lbl.innerText.trim();
        }
        if (el.placeholder) return el.placeholder;
        const txt = el.innerText || el.textContent || '';
        return txt.trim().substring(0, 80);
    }

    function role(el) {
        const ar = el.getAttribute('role');
        if (ar) return ar;
        const tag = el.tagName.toLowerCase();
        if (tag === 'button') return 'button';
        if (tag === 'a') return 'link';
        if (tag === 'select') return 'combobox';
        if (tag === 'textarea') return 'textbox';
        if (tag === 'input') {
            const t = (el.type || '').toLowerCase();
            if (t === 'checkbox') return 'checkbox';
            if (t === 'radio') return 'radio';
            if (t === 'submit' || t === 'button') return 'button';
            return 'textbox';
        }
        return tag;
    }

    function stableSelector(el) {
        if (el.getAttribute('data-testid')) return '[data-testid="' + el.getAttribute('data-testid') + '"]';
        if (el.id) return '#' + CSS.escape(el.id);
        // Build a short CSS path
        let path = '';
        let cur = el;
        while (cur && cur !== document.body && path.split('>').length < 4) {
            let seg = cur.tagName.toLowerCase();
            if (cur.className) {
                const cls = [...cur.classList].slice(0, 2).map(c => '.' + CSS.escape(c)).join('');
                if (cls) seg += cls;
            }
            const siblings = cur.parentElement ? [...cur.parentElement.querySelectorAll(':scope > ' + seg)] : [];
            if (siblings.length > 1) seg += ':nth-of-type(' + (siblings.indexOf(cur) + 1) + ')';
            path = path ? seg + ' > ' + path : seg;
            cur = cur.parentElement;
        }
        return path || el.tagName.toLowerCase();
    }

    function value(el) {
        const t = (el.type || '').toLowerCase();
        if (t === 'password') return '***';
        return el.value !== undefined ? String(el.value) : '';
    }

    const seen = new Set();
    const allEls = [...document.querySelectorAll(SELECTORS)].filter(el => {
        if (seen.has(el)) return false;
        seen.add(el);
        return isVisible(el);
    });

    // Sort: in-viewport first
    allEls.sort((a, b) => (inViewport(b) ? 1 : 0) - (inViewport(a) ? 1 : 0));

    const results = allEls.slice(0, MAX).map((el, i) => {
        const r = el.getBoundingClientRect();
        return {
            idx: i + 1,
            role: role(el),
            name: accessibleName(el),
            placeholder: el.placeholder || null,
            value: value(el),
            bbox: [r.x, r.y, r.width, r.height],
            selector: stableSelector(el),
            visible: inViewport(el),
            enabled: !el.disabled,
        };
    });

    // Visible text summary: headings, alerts, toasts, error messages
    const textNodes = [
        ...document.querySelectorAll('h1,h2,h3,h4,[role="alert"],.alert,.toast,.error,.success,.notification,p.error,span.error,.flash-message')
    ];
    const summary = textNodes
        .map(el => el.innerText.trim())
        .filter(t => t.length > 0 && t.length < 200)
        .slice(0, 10)
        .join(' | ');

    return { elements: results, visible_text: summary };
}
"""


async def ground(page: Page) -> tuple[list[ElementRef], str]:
    """
    Returns (elements, visible_text_summary).
    elements: numbered list of interactive elements, capped at 60
    visible_text_summary: headings, alerts, error messages joined by ' | '
    """
    try:
        raw = await page.evaluate(_GROUND_JS)
    except Exception as e:
        logger.warning("Grounding JS failed: %s", e)
        return [], ""

    elements = []
    for item in raw.get("elements", [])[:80]:
        try:
            el = ElementRef(
                idx=item["idx"],
                role=item["role"],
                name=item["name"] or "",
                placeholder=item.get("placeholder"),
                value=item.get("value"),
                bbox=tuple(item["bbox"]),       # type: ignore[arg-type]
                selector=item["selector"],
                visible=item["visible"],
                enabled=item["enabled"],
            )
            elements.append(el)
        except Exception as e:
            logger.debug("Skipping element: %s", e)

    visible_text = raw.get("visible_text", "")
    return elements, visible_text


def format_elements_for_prompt(elements: list[ElementRef], visible_text: str) -> str:
    """Render elements as the numbered list the LLM expects."""
    lines = []
    for el in elements:
        parts = [f'[{el.idx}] {el.role} "{el.name}"']
        if el.placeholder:
            parts.append(f'placeholder="{el.placeholder}"')
        if el.value:
            parts.append(f'value="{el.value}"')
        if not el.enabled:
            parts.append("disabled")
        lines.append(" ".join(parts))

    if visible_text:
        lines.append(f'VISIBLE TEXT: "{visible_text}"')
    else:
        lines.append("VISIBLE TEXT: (none)")

    return "\n".join(lines)
