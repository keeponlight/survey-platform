// SPDX-License-Identifier: GPL-3.0-or-later
import React from "react";
import { createRoot } from "react-dom/client";

import App from "./App";
import "./styles.css";

const container = document.getElementById("root");
if (container === null) {
  throw new Error("root container #root not found");
}

createRoot(container).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
