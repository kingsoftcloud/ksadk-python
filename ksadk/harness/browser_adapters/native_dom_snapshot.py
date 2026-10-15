"""Bounded host-owned DOM handles, never stored in page globals or attributes."""

from __future__ import annotations

import uuid
from typing import Any

# One page task returns metadata and the exact elements used to produce it.
# Input values stay excluded; page-provided text does not mint reference IDs.
DOM_CAPTURE = r"""() => {
  const semantic = Array.from(document.querySelectorAll(
    'button,input,textarea,select,a[href],[role="button"],[contenteditable="true"]'
  ));
  const pointer = Array.from(document.querySelectorAll('*')).slice(0, 4000)
    .filter((el) => {
      const isPointer = getComputedStyle(el).cursor === 'pointer';
      return isPointer && (!el.parentElement
        || getComputedStyle(el.parentElement).cursor !== 'pointer');
    });
  const nodes = Array.from(new Set([...semantic, ...pointer]));
  const visible = nodes.filter((el) => {
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    const type = (el.getAttribute('type') || '').toLowerCase();
    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden'
      && style.display !== 'none' && type !== 'password' && type !== 'hidden';
  });
  const structuralSelector = (el) => {
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && parts.length < 20) {
      const id = node.id && globalThis.CSS?.escape
        ? '#' + CSS.escape(node.id) : null;
      if (id && document.querySelectorAll(id).length === 1) {
        parts.unshift(id);
        break;
      }
      const tag = node.tagName.toLowerCase();
      const siblings = node.parentElement
        ? Array.from(node.parentElement.children).filter(
          (sibling) => sibling.tagName === node.tagName
        ) : [node];
      parts.unshift(tag + ':nth-of-type(' + (siblings.indexOf(node) + 1) + ')');
      node = node.parentElement;
    }
    if (node && node.parentElement && parts.length >= 20) return null;
    const candidate = parts.join(' > ');
    return candidate && candidate.length <= 512
      && document.querySelectorAll(candidate).length === 1 ? candidate : null;
  };
  const refElements = visible.slice(0, 40);
  const controls = refElements.map((el) => {
    const tag = el.tagName.toLowerCase();
    const rect = el.getBoundingClientRect();
    const inViewport = rect.right > 0 && rect.bottom > 0
      && rect.left < innerWidth && rect.top < innerHeight;
    const hitX = Math.max(0, Math.min(innerWidth - 1,
      (Math.max(0, rect.left) + Math.min(innerWidth, rect.right)) / 2));
    const hitY = Math.max(0, Math.min(innerHeight - 1,
      (Math.max(0, rect.top) + Math.min(innerHeight, rect.bottom)) / 2));
    const hit = inViewport ? document.elementFromPoint(hitX, hitY) : null;
    const id = el.id && globalThis.CSS?.escape ? '#' + CSS.escape(el.id) : null;
    const duplicateId = Boolean(id && document.querySelectorAll(id).length > 1);
    const selector = duplicateId ? null : id || structuralSelector(el);
    const label = el.getAttribute('aria-label') || el.labels?.[0]?.innerText
      || el.getAttribute('placeholder') || el.innerText || '';
    return {
      tag,
      role: el.getAttribute('role') || '',
      label: String(label).trim().slice(0, 160),
      selector,
      duplicate_id: duplicateId,
      clickable: tag === 'button' || el.getAttribute('role') === 'button'
        || getComputedStyle(el).cursor === 'pointer',
      hit_test_reachable: !inViewport ? null : !el.matches(':disabled')
        && (hit === el || el.contains(hit))
    };
  });
  return {refElements, meta: {
    url: location.href,
    title: document.title,
    text: document.body?.innerText || '',
    focus: document.activeElement?.tagName || '',
    scroll_x: Math.round(window.scrollX),
    scroll_y: Math.round(window.scrollY),
    lifecycle: document.readyState,
    pointer_lock_active: Boolean(document.pointerLockElement),
    controls
  }};
}"""


async def _dispose(handles: list[Any]) -> None:
    for handle in handles:
        try:
            await handle.dispose()
        except Exception:
            # Navigation/closing can already have released the execution context.
            pass


class BrowserElementRefs:
    """Latest bounded observation; unchanged DOM nodes retain their opaque ID."""

    def __init__(self) -> None:
        self._elements: dict[str, Any] = {}

    async def clear(self) -> None:
        previous, self._elements = self._elements, {}
        await _dispose(list(previous.values()))

    async def capture(self, page: Any) -> tuple[dict, tuple[str | None, ...]]:
        transient: list[Any] = []
        acquired: list[Any] = []
        adopted = False
        try:
            collection = await page.evaluate_handle(DOM_CAPTURE)
            transient.append(collection)
            metadata = await collection.get_property("meta")
            transient.append(metadata)
            nodes = await collection.get_property("refElements")
            transient.append(nodes)
            meta = await metadata.json_value()
            if not isinstance(meta, dict):
                raise ValueError("invalid DOM snapshot")
            handles: dict[int, Any] = {}
            for index, handle in (await nodes.get_properties()).items():
                acquired.append(handle)
                element = handle.as_element()
                if index.isdecimal() and int(index) < 40 and element is not None:
                    handles[int(index)] = element
            prior_tokens = list(self._elements)
            prior_handles = list(self._elements.values())
            if prior_handles:
                try:
                    same_document = await prior_handles[0].evaluate(
                        "node => node.ownerDocument === document"
                    )
                except Exception:
                    same_document = False
                if not same_document:
                    # A successful click can navigate without _navigate().
                    # Its old execution context cannot supply refs to the new
                    # document; this does not justify replaying that click.
                    await self.clear()
                    prior_tokens, prior_handles = [], []
            # Compare actual node identity in one browser task, without writing
            # refs into an untrusted DOM/global map or one RPC per element pair.
            matches = (
                await nodes.evaluate(
                    "(nodes, prior) => nodes.map(node => prior.indexOf(node))",
                    prior_handles,
                )
                if handles
                else []
            )
            refs: dict[str, Any] = {}
            tokens: list[str | None] = []
            controls = meta.get("controls", [])
            for index in range(min(40, len(controls))):
                element = handles.get(index)
                if element is None:
                    tokens.append(None)
                    continue
                match = matches[index] if index < len(matches) else -1
                token = prior_tokens[match] if 0 <= match < len(prior_tokens) else uuid.uuid4().hex
                refs[token] = element
                tokens.append(token)
            previous, self._elements = self._elements, refs
            adopted = True
            await _dispose(
                list(previous.values()) + [h for h in acquired if h not in refs.values()]
            )
            return meta, tuple(tokens)
        finally:
            if not adopted:
                await _dispose(acquired)
            await _dispose(transient)

    async def resolve(self, token: str) -> Any | None:
        element = self._elements.get(token)
        if element is None:
            return None
        try:
            return element if await element.evaluate("node => node.isConnected") else None
        except Exception:
            return None
