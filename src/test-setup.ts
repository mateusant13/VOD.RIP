import "@testing-library/jest-dom/vitest";
import { configure } from "@testing-library/react";

// Testing Library's async budget (`waitFor`, `findBy*`) is 1000ms and is
// INDEPENDENT of vitest's testTimeout: raising testTimeout alone left that 1s
// cliff standing. Measured across four full runs on a box shared with a game
// and the dev server, the failures were "Unable to find an accessible element
// ..." after a 1s waitFor and sync lookups that raced a mocked fetch - three
// different files, each passing alone. 5s keeps every assertion exactly as it
// was (the element must still appear) and removes the cliff.
configure({ asyncUtilTimeout: 5000 });

// jsdom does not define MediaError; previewPlayerUtils' module self-check
// references it at import time.
const g = globalThis as unknown as { MediaError?: unknown };
if (typeof g.MediaError === "undefined") {
  class MediaErrorShim {
    static readonly MEDIA_ERR_ABORTED = 1;
    static readonly MEDIA_ERR_NETWORK = 2;
    static readonly MEDIA_ERR_DECODE = 3;
    static readonly MEDIA_ERR_SRC_NOT_SUPPORTED = 4;
  }
  g.MediaError = MediaErrorShim;
}
