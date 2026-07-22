import { test } from "node:test";
import assert from "node:assert/strict";
import { selectProvider } from "./provider-selection.js";

test("logged-out default selects the public read-only provider with no configuration", () => {
  const live = { kind: "public-demo" };
  const selected = selectProvider(
    {},
    {
      mockFactory: () => assert.fail("film factory must not run"),
      liveFactory: (...args) => {
        assert.deepEqual(args, []);
        return live;
      },
    },
  );
  assert.equal(selected, live);
});

test("film mode remains an explicit synthetic opt-in", () => {
  const film = { kind: "film" };
  const selected = selectProvider(
    { wantsFilm: true },
    {
      mockFactory: () => film,
      liveFactory: () => assert.fail("public provider must not run"),
    },
  );
  assert.equal(selected, film);
});
