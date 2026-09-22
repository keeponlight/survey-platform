// SPDX-License-Identifier: GPL-3.0-or-later
import { useEffect, useState } from "react";

import { SimBadge } from "./components";
import RunCreate from "./pages/RunCreate";
import RunDetail from "./pages/RunDetail";
import RunResults from "./pages/RunResults";
import SurveyEditor from "./pages/SurveyEditor";
import { parseRoute, routes, type Route } from "./router";

function NavLink({ label, active, onClick }: { label: string; active: boolean; onClick: () => void }): JSX.Element {
  return (
    <button type="button" className={active ? "primary" : ""} onClick={onClick}>
      {label}
    </button>
  );
}

export default function App(): JSX.Element {
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.hash));

  useEffect(() => {
    const onChange = (): void => setRoute(parseRoute(window.location.hash));
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);

  return (
    <div>
      <header className="app-header">
        <span className="brand">万级智能体模拟问卷平台</span>
        <SimBadge />
      </header>

      <nav className="app-nav">
        <NavLink
          label="问卷列表 / 编辑"
          active={route.page === "surveys"}
          onClick={() => routes.surveys()}
        />
        <NavLink
          label="上传与运行配置"
          active={route.page === "run-create"}
          onClick={() => routes.runCreate()}
        />
        {route.page === "run-detail" ? (
          <NavLink label="运行详情" active onClick={() => routes.runDetail(route.runId)} />
        ) : null}
        {route.page === "run-results" ? (
          <NavLink label="结果页" active onClick={() => routes.runResults(route.runId)} />
        ) : null}
      </nav>

      <main className="main">
        {route.page === "surveys" ? <SurveyEditor /> : null}
        {route.page === "run-create" ? <RunCreate initialSurveyId={route.surveyId} /> : null}
        {route.page === "run-detail" ? <RunDetail runId={route.runId} /> : null}
        {route.page === "run-results" ? <RunResults runId={route.runId} /> : null}
      </main>
    </div>
  );
}
