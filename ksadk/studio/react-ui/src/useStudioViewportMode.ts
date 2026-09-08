import { useEffect, useState } from "react";
import {
  viewportModeForWidth,
  type StudioViewportMode,
} from "./responsiveViewport";

export function useStudioViewportMode(): StudioViewportMode {
  const [mode, setMode] = useState<StudioViewportMode>(() => (
    viewportModeForWidth(window.innerWidth)
  ));

  useEffect(() => {
    const updateMode = () => setMode(viewportModeForWidth(window.innerWidth));
    window.addEventListener("resize", updateMode);
    return () => window.removeEventListener("resize", updateMode);
  }, []);

  return mode;
}
