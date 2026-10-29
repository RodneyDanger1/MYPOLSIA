/**
 * WAVE 4 / F-32 — the create response's `publishing_warnings` is rendered where it was earned.
 *
 * THE DEFECT (bridge STOREFRONT-CONTRACT §3 / R-W2-B2): `POST /api/v1/sites` writes
 * `contact.html` with the profile's postal address, legal name and contact email verbatim, and an
 * API client got `{slug,url,bytes}` with no warning. The backend half of this wave adds
 * `publishing_warnings: [{code, message, fields, how_to_fix}]`; this suite pins the screen half —
 * the warning is shown ABOVE the site rows, in a `data-state="warning"` block, with the fields the
 * API named and its fix hint. It appears only when the server declared something: an older server
 * (no key) and a malformed entry render nothing rather than a half-invented warning.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn(), post: jest.fn(), put: jest.fn(), delete: jest.fn() } };
});

import { api } from "@/lib/api";
import SitesPage from "@/app/sites/page";

const mockGet = api.get as jest.Mock;
const mockPost = api.post as jest.Mock;

const WARNING = {
  code: "W_PUBLIC_CONTACT_DETAILS",
  message:
    "contact.html publishes your stored postal address, your stored legal name and a mailto: link to your contact email verbatim and unredacted, so anyone who can reach this page can read them.",
  fields: ["sender_postal_address", "sender_legal_name", "notification_email"],
  how_to_fix:
    "clear the named fields in Settings → Company if this site is a draft that must not publish them",
};

const CREATED = {
  slug: "contact-disclosure",
  url: "/sites/contact-disclosure/",
  bytes: 9135,
};

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
  // The list read: one empty page on mount (and again after the create's reload).
  mockGet.mockResolvedValue([]);
});

async function createSite(): Promise<void> {
  const user = userEvent.setup();
  render(<SitesPage />);
  await screen.findByTestId("sites-create-form");
  await user.click(screen.getByRole("button", { name: /create site/i }));
  await waitFor(() => expect(mockPost).toHaveBeenCalledWith("/sites", {}));
}

it("renders the server's warning above the site rows", async () => {
  mockPost.mockResolvedValueOnce({ ...CREATED, publishing_warnings: [WARNING] });

  await createSite();

  const block = await screen.findByTestId("sites-publishing-warnings");
  expect(block).toHaveAttribute("data-state", "warning");
  expect(block).toHaveTextContent("W_PUBLIC_CONTACT_DETAILS");
  expect(block).toHaveTextContent(/contact\.html publishes/);
  expect(block).toHaveTextContent(/published fields: sender_postal_address, sender_legal_name, notification_email/);
  expect(block).toHaveTextContent(/Settings → Company/);
  // The warning is a sibling of the create card, BEFORE the list region.
  const form = screen.getByTestId("sites-create-form");
  expect(
    form.compareDocumentPosition(block) & Node.DOCUMENT_POSITION_FOLLOWING,
  ).toBeTruthy();
});

it("an empty list is a real 'nothing published' answer: no block", async () => {
  mockPost.mockResolvedValueOnce({ ...CREATED, publishing_warnings: [] });

  await createSite();

  await waitFor(() => expect(screen.queryByTestId("sites-publishing-warnings")).toBeNull());
});

it("a pre-F-32 server that omits the key shows no warning (and no invented one)", async () => {
  mockPost.mockResolvedValueOnce(CREATED);

  await createSite();

  await waitFor(() => expect(screen.queryByTestId("sites-publishing-warnings")).toBeNull());
});

it("a malformed entry is dropped instead of rendering half a warning", async () => {
  mockPost.mockResolvedValueOnce({
    ...CREATED,
    publishing_warnings: [{ code: 7, message: null }, "not an object"],
  });

  await createSite();

  await waitFor(() => expect(screen.queryByTestId("sites-publishing-warnings")).toBeNull());
});
