"""Serialize visible form controls from the page for LLM / CLI output."""

from __future__ import annotations

import json
from typing import Any

# Run in browser: collect inputs, selects, textareas with label hints.
_FIELD_SCANNER_JS = """
() => {
  function visible(el) {
    if (!el || !el.getBoundingClientRect) return false;
    const s = window.getComputedStyle(el);
    if (s.display === "none" || s.visibility === "hidden" || s.opacity === "0")
      return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }
  function labelFor(el) {
    if (el.labels && el.labels.length) {
      return Array.from(el.labels).map((l) => l.innerText.trim()).filter(Boolean).join(" | ");
    }
    const id = el.getAttribute("id");
    if (id) {
      const lab = document.querySelector(`label[for="${CSS.escape(id)}"]`);
      if (lab) return lab.innerText.trim();
    }
    let p = el.parentElement;
    for (let i = 0; i < 6 && p; i++, p = p.parentElement) {
      const prev = p.previousElementSibling;
      if (prev && /^H[1-6]$/.test(prev.tagName)) return prev.innerText.trim();
    }
    return "";
  }
  function sectionHint(el) {
    let n = el;
    for (let i = 0; i < 8 && n; i++, n = n.parentElement) {
      const h = n.querySelector && n.querySelector(":scope > h1, :scope > h2, :scope > h3");
      if (h && visible(h)) return h.innerText.trim();
    }
    return "";
  }
  const selectors = "input, select, textarea";
  const out = [];
  const seen = new Set();
  document.querySelectorAll(selectors).forEach((el, idx) => {
    if (!visible(el)) return;
    const tag = el.tagName.toLowerCase();
    const inpType = tag === "input" ? (el.type || "text").toLowerCase() : tag;
    let dedupe;
    if (inpType === "radio" || inpType === "checkbox") {
      dedupe = `${inpType}:${el.name || ""}:${el.value || ""}:${idx}`;
    } else {
      const key = el.name || el.id || `${tag}_${idx}`;
      dedupe = `${key}:${inpType}:${idx}`;
    }
    if (seen.has(dedupe)) return;
    seen.add(dedupe);
    const type = inpType;
    const entry = {
      tag,
      type,
      name: el.name || null,
      id: el.id || null,
      label: labelFor(el) || null,
      section_hint: sectionHint(el) || null,
      placeholder: el.getAttribute("placeholder"),
      required: !!el.required,
      disabled: !!el.disabled,
      max: el.max != null && el.max !== "" ? el.max : null,
      min: el.min != null && el.min !== "" ? el.min : null,
      step: el.step != null && el.step !== "" ? el.step : null,
    };
    if (tag === "select") {
      entry.options = Array.from(el.options).map((o) => ({
        value: o.value,
        text: o.innerText.trim(),
        selected: o.selected,
      }));
    }
    if (type === "radio" || type === "checkbox") {
      entry.checked = !!el.checked;
      entry.value = el.value;
    }
    out.push(entry);
  });
  return out;
}
"""


def collect_form_fields_json(page) -> list[dict[str, Any]]:
    raw: list[dict[str, Any]] = page.evaluate(_FIELD_SCANNER_JS)
    return raw


def format_fields_human_readable(fields: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for i, f in enumerate(fields, 1):
        label = f.get("label") or f.get("name") or f.get("id") or f.get("type")
        sec = f.get("section_hint")
        head = f"[{i}] {f.get('tag')}/{f.get('type')}"
        if sec:
            head += f"  (near: {sec[:80]})"
        lines.append(head)
        lines.append(f"    label: {label}")
        if f.get("name"):
            lines.append(f"    name: {f['name']}")
        if f.get("id"):
            lines.append(f"    id: {f['id']}")
        if f.get("type") in ("number", "range") or f.get("tag") == "textarea":
            for k in ("min", "max", "step"):
                if f.get(k) is not None:
                    lines.append(f"    {k}: {f[k]}")
        if f.get("options"):
            lines.append("    options:")
            for opt in f["options"][:20]:
                sel = " *" if opt.get("selected") else ""
                lines.append(f"      - {opt.get('value')!r}: {opt.get('text')}{sel}")
            if len(f["options"]) > 20:
                lines.append(f"      ... ({len(f['options']) - 20} more)")
        if f.get("type") in ("radio", "checkbox"):
            lines.append(f"    checked: {f.get('checked')}  value: {f.get('value')!r}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def fields_to_json_text(fields: list[dict[str, Any]], *, indent: int = 2) -> str:
    return json.dumps(fields, indent=indent, ensure_ascii=False) + "\n"
