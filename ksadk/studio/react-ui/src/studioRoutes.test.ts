import { describe, expect, it, vi } from "vitest";

import {
  deploymentCreateRoute,
  deploymentDetailRoute,
  navigateToStudioHash,
} from "./studioRoutes";

describe("Studio delivery routes", () => {
  it("binds a deployment flow to the exact successful Build", () => {
    expect(deploymentCreateRoute("build/one", "agent one")).toBe(
      "#/deployments/new?buildId=build%2Fone&agentId=agent+one",
    );
  });

  it("encodes deployment detail identifiers", () => {
    expect(deploymentDetailRoute("dep/one")).toBe("#/deployments/dep%2Fone");
  });

  it("notifies the router when navigating to the current hash", () => {
    window.location.hash = "#/deployments";
    const listener = vi.fn();
    window.addEventListener("hashchange", listener);

    navigateToStudioHash("#/deployments");

    expect(listener).toHaveBeenCalledTimes(1);
    window.removeEventListener("hashchange", listener);
  });
});
