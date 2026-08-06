const BACKEND_URL = "http://127.0.0.1:8730";

const tokenInput = document.getElementById("token") as HTMLInputElement;
const testButton = document.getElementById("test-connection") as HTMLButtonElement;
const result = document.getElementById("result") as HTMLParagraphElement;

chrome.storage.local.get("pairingToken", ({ pairingToken }) => {
  if (typeof pairingToken === "string") {
    tokenInput.value = pairingToken;
  }
});

tokenInput.addEventListener("change", () => {
  chrome.storage.local.set({ pairingToken: tokenInput.value });
});

testButton.addEventListener("click", async () => {
  result.textContent = "Testing...";
  try {
    const response = await fetch(`${BACKEND_URL}/api/health`, {
      headers: { Authorization: `Bearer ${tokenInput.value}` },
    });
    const data = await response.json();
    result.textContent = `status: ${data.status}, paired: ${data.paired}`;
  } catch {
    result.textContent = "unreachable";
  }
});
