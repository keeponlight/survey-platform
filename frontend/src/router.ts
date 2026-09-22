// SPDX-License-Identifier: GPL-3.0-or-later
/** 极简 hash 路由（不引入路由库，符合「不引重型依赖」约定）。 */

export type Route =
  | { page: "surveys" }
  | { page: "run-create"; surveyId: string | null }
  | { page: "run-detail"; runId: string }
  | { page: "run-results"; runId: string };

export function parseRoute(hash: string): Route {
  const raw = hash.startsWith("#") ? hash.slice(1) : hash;
  const [pathPart = "", queryPart = ""] = raw.split("?");
  const segments = pathPart.split("/").filter((segment) => segment !== "");
  const query = new URLSearchParams(queryPart);

  if (segments[0] === "runs") {
    if (segments[1] === "new") {
      return { page: "run-create", surveyId: query.get("survey_id") };
    }
    if (segments[1] !== undefined && segments[2] === "results") {
      return { page: "run-results", runId: segments[1] };
    }
    if (segments[1] !== undefined) {
      return { page: "run-detail", runId: segments[1] };
    }
    return { page: "run-create", surveyId: null };
  }
  return { page: "surveys" };
}

export function navigate(to: string): void {
  const target = to.startsWith("#") ? to : `#${to}`;
  if (window.location.hash === target) return;
  window.location.hash = target;
}

export const routes = {
  surveys: () => navigate("/surveys"),
  runCreate: (surveyId?: string) =>
    navigate(surveyId !== undefined ? `/runs/new?survey_id=${encodeURIComponent(surveyId)}` : "/runs/new"),
  runDetail: (runId: string) => navigate(`/runs/${runId}`),
  runResults: (runId: string) => navigate(`/runs/${runId}/results`),
};
