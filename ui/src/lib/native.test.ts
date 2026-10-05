import { describe, expect, it } from "vitest";
import { nativeMatches } from "$lib/native";

const A = { provisioned: true, userId: "1", origin: "https://home.example" };

describe("native provisioning identity", () => {
  it("requires the authenticated user's stable ID", () => {
    expect(nativeMatches(A, "1", A.origin)).toBe(true);
    expect(nativeMatches(A, "2", A.origin)).toBe(false);
    expect(nativeMatches(A, undefined, A.origin)).toBe(false);
  });

  it("does not reuse another instance's provisioning, including a different port", () => {
    expect(nativeMatches(A, "1", "https://cabin.example")).toBe(false);
    expect(nativeMatches(A, "1", "https://home.example:8443")).toBe(false);
  });

  it("requires complete active provisioning", () => {
    expect(nativeMatches({ ...A, provisioned: false }, "1", A.origin)).toBe(false);
    expect(nativeMatches({ ...A, userId: null }, "1", A.origin)).toBe(false);
    expect(nativeMatches(null, "1", A.origin)).toBe(false);
  });
});
