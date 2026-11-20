"""A small typed builder for FFmpeg filter graphs (DS 5.5).

Filters are built from a name and options; values are quoted and escaped here, and pad
labels are generated, so the final ``-filter_complex`` string is always well formed.
Nothing from the Edit Spec reaches FFmpeg except through these builders (RND-1, NFR-S4).
"""

from __future__ import annotations

from dataclasses import dataclass, field

_SPECIAL = set("\\':,;[]=")


def value(v: object) -> str:
    """One option value, quoted when it holds characters the graph syntax uses."""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float):
        text = f"{v:.6f}".rstrip("0").rstrip(".")
        return text if text not in ("", "-0") else "0"
    text = str(v)
    if not any(c in _SPECIAL for c in text):
        return text
    return "'" + text.replace("'", "'\\''") + "'"


def f(name: str, *args: object, **opts: object) -> str:
    """``f("scale", 1080, 1920, flags="lanczos")`` → ``scale=1080:1920:flags=lanczos``."""
    parts = [value(a) for a in args] + [f"{k}={value(v)}" for k, v in opts.items()
                                         if v is not None]
    return f"{name}={':'.join(parts)}" if parts else name


@dataclass
class Graph:
    chains: list[str] = field(default_factory=list)
    _count: int = 0

    def label(self, prefix: str) -> str:
        self._count += 1
        return f"{prefix}{self._count}"

    def chain(self, inputs: list[str], filters: list[str], outputs: int | list[str] = 1,
              prefix: str = "p") -> list[str]:
        """Add ``[in]…filter,filter…[out]``; returns the output labels."""
        outs = outputs if isinstance(outputs, list) else [self.label(prefix)
                                                           for _ in range(outputs)]
        ins = "".join(f"[{i}]" for i in inputs)
        self.chains.append(f"{ins}{','.join(filters)}{''.join(f'[{o}]' for o in outs)}")
        return outs

    def one(self, inputs: list[str], filters: list[str], prefix: str = "p") -> str:
        return self.chain(inputs, filters, 1, prefix)[0]

    def render(self) -> str:
        return ";".join(self.chains)
