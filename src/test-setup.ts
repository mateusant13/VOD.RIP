import "@testing-library/jest-dom/vitest";

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
