import { createRoot } from "react-dom/client";
import App from "./App";
import "./index.css";
import { AppErrorBoundary } from "@/components/AppErrorBoundary";

createRoot(document.getElementById("root")!).render(
	<AppErrorBoundary>
		<App />
	</AppErrorBoundary>,
);
