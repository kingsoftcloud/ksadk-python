import type { HTMLAttributes } from "react";
import "../king-icons.css";

// Official King Design code points from ksc-fe/kpc/styles/fonts/iconfont.ts.
const KING_ICON_GLYPHS = {
  add: "\ue970",
  all: "\ue9a9",
  check: "\ue96d",
  close: "\ue972",
  cpu: "\ue9be",
  down: "\ue964",
  folder: "\ue994",
  "left-squared": "\ue975",
  message: "\ue9a4",
  panel: "\ue99f",
  refresh: "\ue989",
  right: "\ue966",
  "right-squared": "\ue976",
  search: "\ue97e",
  settings: "\ue992",
  users: "\ue999",
} as const;

export type KingIconName = keyof typeof KING_ICON_GLYPHS;

interface KingIconProps extends Omit<HTMLAttributes<HTMLSpanElement>, "children"> {
  name: KingIconName;
  size?: number;
}

/** Decorative icons; the containing action supplies its accessible name. */
export function KingIcon({ name, size = 16, className = "", style, ...props }: KingIconProps) {
  return (
    <span
      {...props}
      className={`king-icon${className ? ` ${className}` : ""}`}
      data-icon={name}
      data-glyph={KING_ICON_GLYPHS[name]}
      aria-hidden="true"
      style={{ fontSize: size, width: size, height: size, ...style }}
    />
  );
}
