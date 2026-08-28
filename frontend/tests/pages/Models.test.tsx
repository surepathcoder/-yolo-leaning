/**
 * The Models page (milestone 15): the mAP activation gate and the
 * one-active-per-name / archive flow have to be visible and operable here,
 * not just enforced server-side.
 */

import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/services/endpoints", () => ({
  cameraApi: { list: vi.fn() },
  modelApi: { list: vi.fn(), activate: vi.fn(), archive: vi.fn(), assignCamera: vi.fn() },
}));

import { ModelsPage } from "@/pages/Models";
import { cameraApi, modelApi } from "@/services/endpoints";
import { useAuth } from "@/stores/auth";

import { cameraSummary, currentUser, modelVersion, page } from "../fixtures";
import { renderWithProviders } from "../test-utils";

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(cameraApi.list).mockResolvedValue(page([]));
});

describe("model list", () => {
  it("renders each version's status and mAP50", async () => {
    vi.mocked(modelApi.list).mockResolvedValue([
      modelVersion({ id: "m1", name: "shelf-products", version: "v1", status: "ACTIVE", map50: 0.75 }),
      modelVersion({
        id: "m2",
        name: "checkout-scanner",
        version: "v2",
        status: "VALIDATED",
        map50: 0.2,
        activatable: false,
      }),
    ]);
    useAuth.setState({ user: currentUser({ permissions: ["model:activate"] }) });

    renderWithProviders(<ModelsPage />);

    expect(await screen.findByText("shelf-products")).toBeInTheDocument();
    expect(screen.getByText("checkout-scanner")).toBeInTheDocument();
    expect(screen.getByText("0.750")).toBeInTheDocument();
    expect(screen.getByText("0.200")).toBeInTheDocument();
    expect(screen.getByText("ACTIVE")).toBeInTheDocument();
    expect(screen.getByText("VALIDATED")).toBeInTheDocument();
  });

  it("shows the empty state when no models are registered", async () => {
    vi.mocked(modelApi.list).mockResolvedValue([]);
    useAuth.setState({ user: currentUser() });

    renderWithProviders(<ModelsPage />);

    expect(await screen.findByText("No models yet")).toBeInTheDocument();
  });

  it("disables Activate below the mAP gate and explains why", async () => {
    vi.mocked(modelApi.list).mockResolvedValue([
      modelVersion({ id: "weak", status: "VALIDATED", map50: 0.2, activatable: false }),
    ]);
    useAuth.setState({ user: currentUser({ permissions: ["model:activate"] }) });

    renderWithProviders(<ModelsPage />);

    const activateButton = await screen.findByRole("button", { name: /activate/i });
    expect(activateButton).toBeDisabled();
    expect(activateButton).toHaveAttribute("title", "mAP50 is below the activation threshold");
  });

  it("activates a model and refreshes the list", async () => {
    vi.mocked(modelApi.list).mockResolvedValue([
      modelVersion({ id: "m1", status: "VALIDATED", map50: 0.62, activatable: true }),
    ]);
    vi.mocked(modelApi.activate).mockResolvedValue(
      modelVersion({ id: "m1", status: "ACTIVE", map50: 0.62 }),
    );
    useAuth.setState({ user: currentUser({ permissions: ["model:activate"] }) });

    renderWithProviders(<ModelsPage />);
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: /activate/i }));

    await waitFor(() => expect(modelApi.activate).toHaveBeenCalledWith("m1"));
    await waitFor(() => expect(vi.mocked(modelApi.list).mock.calls.length).toBeGreaterThan(1));
  });

  it("offers Archive instead of Activate for the active version", async () => {
    vi.mocked(modelApi.list).mockResolvedValue([modelVersion({ id: "m1", status: "ACTIVE" })]);
    useAuth.setState({ user: currentUser({ permissions: ["model:activate"] }) });

    renderWithProviders(<ModelsPage />);

    expect(await screen.findByRole("button", { name: /archive/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^activate$/i })).not.toBeInTheDocument();
  });

  it("hides both action buttons without model:activate", async () => {
    vi.mocked(modelApi.list).mockResolvedValue([modelVersion({ id: "m1", status: "VALIDATED" })]);
    useAuth.setState({ user: currentUser({ permissions: [] }) });

    renderWithProviders(<ModelsPage />);

    await screen.findByText("shelf-products");
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});

describe("camera assignment", () => {
  it("is hidden without camera:write", async () => {
    vi.mocked(modelApi.list).mockResolvedValue([]);
    useAuth.setState({ user: currentUser({ permissions: [] }) });

    renderWithProviders(<ModelsPage />);

    await screen.findByText("No models yet");
    expect(screen.queryByText("Camera assignments")).not.toBeInTheDocument();
  });

  it("assigns an active model to a camera", async () => {
    vi.mocked(modelApi.list).mockResolvedValue([
      modelVersion({ id: "m1", name: "shelf-products", version: "v1", status: "ACTIVE" }),
    ]);
    vi.mocked(cameraApi.list).mockResolvedValue(
      page([cameraSummary({ id: "cam-1", name: "Aisle 3", model_version_id: null })]),
    );
    vi.mocked(modelApi.assignCamera).mockResolvedValue({ message: "assigned" });
    useAuth.setState({ user: currentUser({ permissions: ["model:activate", "camera:write"] }) });

    renderWithProviders(<ModelsPage />);
    const user = userEvent.setup();

    const cameraRow = (await screen.findByText("Aisle 3")).closest("tr");
    if (!cameraRow) throw new Error("camera row not found");
    const select = within(cameraRow).getByRole("combobox");

    await user.selectOptions(select, "m1");

    await waitFor(() => expect(modelApi.assignCamera).toHaveBeenCalledWith("m1", "cam-1"));
  });
});
