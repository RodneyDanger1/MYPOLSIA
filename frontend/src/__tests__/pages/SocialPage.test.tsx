import { render, screen } from "@testing-library/react";

jest.mock("@/lib/api", () => ({ api: { get: jest.fn() } }));

import { api } from "@/lib/api";
import SocialPage from "@/app/social/page";

const mockApiGet = api.get as jest.MockedFunction<typeof api.get>;

/** The page's own `Post` wire shape: `engagement` is `dict[str, Any] | None` server-side
 *  (`SocialPostResponse.engagement`, `app/schemas/social.py`), i.e. untyped per counter. */
type WirePost = {
  id: number;
  content: string;
  status: string;
  published_at: string | null;
  engagement: Record<string, unknown> | null;
};

// Wave U2 (coder F2 restyle): the card is still found by its text, but the element is the
// restyled `Card` (`data-testid="social-post"`) instead of the retired `div.bg-gray-800`.
// Old -> new recorded in wave-u2/coder-f2-social-outreach/RECEIPT.md.
describe("SocialPage", () => {
  afterEach(() => mockApiGet.mockReset());

  it("renders a null engagement as the em dash", async () => {
    const post: WirePost = {
      id: 21,
      content: "fixture post without counters",
      status: "draft",
      published_at: null,
      engagement: null,
    };
    mockApiGet.mockResolvedValue([post]);

    render(<SocialPage />);

    const card = (await screen.findByText(post.content)).closest('[data-testid="social-post"]');
    expect(card).not.toBeNull();
    expect(card).toHaveTextContent("engagement: —");
    expect(card).not.toHaveTextContent("[object Object]");
  });

  it("renders numeric counters verbatim and a non-numeric value honestly", async () => {
    const post: WirePost = {
      id: 22,
      content: "fixture post with counters",
      status: "published",
      published_at: "2026-10-05T12:30:00Z",
      // The API carries whatever the platform's payload held: numbers, strings, missing values.
      engagement: { likes: 11, reposts: 2, source: "twitter", retweets: null },
    };
    mockApiGet.mockResolvedValue([post]);

    render(<SocialPage />);

    const card = (await screen.findByText(post.content)).closest('[data-testid="social-post"]');
    expect(card).not.toBeNull();
    // These two exact strings are what e2e/social.spec.ts asserts on the rendered page.
    expect(card).toHaveTextContent("likes: 11");
    expect(card).toHaveTextContent("reposts: 2");
    expect(card).toHaveTextContent("source: twitter");
    expect(card).toHaveTextContent("retweets: —");
    expect(card).not.toHaveTextContent("[object Object]");
  });
});
