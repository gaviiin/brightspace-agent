import { useEffect, useState } from "react";

type HealthState = "loading" | "ok" | "unreachable";

interface HealthResponse {
  status: string;
  version: string;
  paired: boolean;
}

function App() {
  const [health, setHealth] = useState<HealthState>("loading");

  useEffect(() => {
    fetch("/api/health")
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json() as Promise<HealthResponse>;
      })
      .then((data) => setHealth(data.status === "ok" ? "ok" : "unreachable"))
      .catch(() => setHealth("unreachable"));
  }, []);

  return (
    <div>
      <h1>BrightSpace Agent</h1>
      <p>Backend status: {health}</p>
    </div>
  );
}

export default App;
