import { describe, expect, it } from "vitest";
import { parseProviderConfig } from "./agentProviders";

describe("AgentProvider config secret references", () => {
  it("enforces manifest-declared secret fields even when their name is not heuristic", () => {
    expect(() => parseProviderConfig(
      '{"connectionRef":"plain-credential"}',
      ["connectionRef"],
    )).toThrow(/只能填写 Secret 引用/);

    expect(parseProviderConfig(
      '{"connectionRef":"env://PROVIDER_CONNECTION","nested":{"connectionRef":"secret://provider/account"}}',
      ["connectionRef"],
    )).toEqual({
      connectionRef: "env://PROVIDER_CONNECTION",
      nested: { connectionRef: "secret://provider/account" },
    });
  });
});
