/**
 * MiroFish TypeScript Client Reference
 * ======================================
 * Shows how to call the MiroFish REST API from a TypeScript / Node.js service.
 * Works with Next.js API routes, Express, Fastify, or plain Node.
 *
 * Prerequisites:
 *   - MiroFish backend running: cd backend && python run.py
 *   - Node 18+ (built-in fetch) or install node-fetch
 *   - ts-node for direct execution: npx ts-node example_ts_client.ts
 */

const MIROFISH_URL = "http://localhost:5001";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface Project {
  project_id: string;
  status: string;
  graph_id?: string;
  error?: string;
}

interface Task {
  task_id: string;
  status: "PENDING" | "PROCESSING" | "COMPLETED" | "FAILED";
  progress: number;
  error?: string;
}

interface Simulation {
  simulation_id: string;
  status: string;
  error?: string;
}

interface RunStatus {
  simulation_id: string;
  runner_status: "running" | "completed" | "stopped" | "failed";
  progress_percent: number;
  current_round: number;
  total_rounds: number;
}

interface Report {
  content: string;
  status: string;
}

// ---------------------------------------------------------------------------
// HTTP helpers
// ---------------------------------------------------------------------------

async function apiGet<T>(path: string): Promise<T> {
  const res = await fetch(`${MIROFISH_URL}/api${path}`);
  const json = await res.json();
  if (!res.ok) throw new Error(`API ${res.status}: ${JSON.stringify(json)}`);
  return json as T;
}

async function apiPost<T>(path: string, body?: object): Promise<T> {
  const res = await fetch(`${MIROFISH_URL}/api${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  const json = await res.json();
  if (!res.ok) throw new Error(`API ${res.status}: ${JSON.stringify(json)}`);
  return json as T;
}

async function uploadFiles(
  path: string,
  files: Array<{ name: string; content: Blob | Buffer }>,
  requirement: string
): Promise<Record<string, unknown>> {
  const form = new FormData();
  for (const file of files) {
    form.append("files", new Blob([file.content]), file.name);
  }
  form.append("requirement", requirement);

  const res = await fetch(`${MIROFISH_URL}/api${path}`, {
    method: "POST",
    body: form,
  });
  const json = await res.json();
  if (!res.ok) throw new Error(`Upload error ${res.status}: ${JSON.stringify(json)}`);
  return json as Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// Polling helpers
// ---------------------------------------------------------------------------

async function pollTask(taskId: string, intervalMs = 3000, timeoutMs = 180000): Promise<Task> {
  const deadline = Date.now() + timeoutMs;
  while (true) {
    const result = await apiGet<{ task: Task }>(`/graph/status/${taskId}`);
    const task = result.task;
    console.log(`  task ${taskId}: status=${task.status} progress=${task.progress}%`);
    if (task.status === "COMPLETED" || task.status === "FAILED") return task;
    if (Date.now() > deadline) throw new Error(`Timeout polling task ${taskId}`);
    await sleep(intervalMs);
  }
}

async function pollSimulation(simId: string, intervalMs = 30000, timeoutMs = 3600000): Promise<RunStatus> {
  const deadline = Date.now() + timeoutMs;
  while (true) {
    const status = await apiGet<RunStatus>(`/simulation/${simId}/run_status`);
    console.log(
      `  simulation: ${status.runner_status} round ${status.current_round}/${status.total_rounds} (${status.progress_percent.toFixed(0)}%)`
    );
    if (["completed", "stopped", "failed"].includes(status.runner_status)) return status;
    if (Date.now() > deadline) throw new Error(`Timeout waiting for simulation ${simId}`);
    await sleep(intervalMs);
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// ---------------------------------------------------------------------------
// Main: cart recovery pipeline
// ---------------------------------------------------------------------------

/**
 * Shopify cart abandonment data shape.
 * Adapt this to your actual Shopify webhook payload.
 */
interface ShopifyCartPayload {
  customerId: string;
  customerName: string;
  email: string;
  cartItems: Array<{ product: string; price: number; quantity: number }>;
  cartTotal: number;
  exitPage: string;
  abandonedAtStep: string;
  pastOrders: number;
  device: string;
  location: string;
}

/**
 * Converts Shopify cart payload to a MiroFish seed document string.
 */
function buildSeedDocument(cart: ShopifyCartPayload): string {
  const itemsText = cart.cartItems
    .map((i) => `  - ${i.product} × ${i.quantity} @ $${i.price.toFixed(2)}`)
    .join("\n");

  return `Customer Profile: ${cart.customerName}

${cart.customerName} is a ${cart.pastOrders === 0 ? "new customer" : `returning customer (${cart.pastOrders} previous orders)`} from ${cart.location}.
They accessed the store via ${cart.device}.

Abandoned Cart Contents:

${itemsText}
Cart total: $${cart.cartTotal.toFixed(2)}

Abandonment Context:

They abandoned at the ${cart.abandonedAtStep} step (last page: ${cart.exitPage}).

Social Context:

Similar Shopper A is price-conscious and compares prices across sites before buying.
Similar Shopper B responds strongly to urgency signals like limited stock warnings.
Brand Advocate is a loyal repeat customer who recommends this store frequently.`;
}

async function runCartRecoveryPipeline(cart: ShopifyCartPayload): Promise<string> {
  const requirement =
    "Simulate the psychology of a customer who abandoned their shopping cart. " +
    "Predict the emotional and rational reasons for abandonment, identify key objections, " +
    "and determine the most effective messaging angle for a personalised recovery email.";

  const seedDoc = buildSeedDocument(cart);

  // Step 1: Upload seed doc + generate ontology
  console.log("Step 1: Generating ontology...");
  const seedBlob = Buffer.from(seedDoc, "utf-8");
  const ontologyResult = await uploadFiles("/graph/ontology/generate", [{ name: "cart_seed.txt", content: seedBlob }], requirement);
  const project = ontologyResult.project as Project;
  console.log(`  project_id: ${project.project_id}`);

  // Step 2: Build knowledge graph
  console.log("Step 2: Building knowledge graph...");
  const buildResult = await apiPost<{ task_id: string }>(`/graph/build/${project.project_id}`);
  const task = await pollTask(buildResult.task_id);
  if (task.status === "FAILED") throw new Error(`Graph build failed: ${task.error}`);

  // Step 3: Create simulation
  console.log("Step 3: Creating simulation...");
  const simResult = await apiPost<{ simulation: Simulation }>("/simulation/create", {
    project_id: project.project_id,
    simulation_requirement: requirement,
    enable_twitter: true,
    enable_reddit: false,
  });
  const simulation = simResult.simulation;
  console.log(`  simulation_id: ${simulation.simulation_id}`);

  // Step 4: Prepare simulation
  console.log("Step 4: Preparing simulation...");
  await apiPost(`/simulation/${simulation.simulation_id}/prepare`);

  const prepDeadline = Date.now() + 20 * 60 * 1000;
  while (true) {
    const prepStatus = await apiGet<{ status: string }>(`/simulation/${simulation.simulation_id}/prepare_status`);
    console.log(`  prepare status: ${prepStatus.status}`);
    if (prepStatus.status === "READY") break;
    if (prepStatus.status === "FAILED") throw new Error("Simulation preparation failed");
    if (Date.now() > prepDeadline) throw new Error("Timeout during simulation preparation");
    await sleep(10000);
  }

  // Step 5: Start simulation
  console.log("Step 5: Starting simulation...");
  await apiPost(`/simulation/${simulation.simulation_id}/start`);
  const runStatus = await pollSimulation(simulation.simulation_id);
  console.log(`Simulation finished: ${runStatus.runner_status}`);

  // Step 6: Generate report
  console.log("Step 6: Generating report...");
  await apiPost(`/report/${simulation.simulation_id}/generate`);

  const reportDeadline = Date.now() + 5 * 60 * 1000;
  while (true) {
    const reportStatus = await apiGet<{ status: string }>(`/report/${simulation.simulation_id}/status`);
    console.log(`  report status: ${reportStatus.status}`);
    if (reportStatus.status === "completed") break;
    if (reportStatus.status === "failed") throw new Error("Report generation failed");
    if (Date.now() > reportDeadline) throw new Error("Timeout during report generation");
    await sleep(5000);
  }

  // Step 7: Fetch report
  const report = await apiGet<{ content: string }>(`/report/${simulation.simulation_id}/full`);
  return report.content;
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

const mockCart: ShopifyCartPayload = {
  customerId: "cust_7821",
  customerName: "Sarah Mitchell",
  email: "sarah@example.com",
  cartItems: [
    { product: "Wireless Noise-Cancelling Headphones", price: 149.99, quantity: 1 },
    { product: "USB-C Cable 3-pack", price: 19.99, quantity: 1 },
  ],
  cartTotal: 169.98,
  exitPage: "checkout/payment",
  abandonedAtStep: "payment",
  pastOrders: 2,
  device: "mobile",
  location: "Austin, TX, USA",
};

runCartRecoveryPipeline(mockCart)
  .then((report) => {
    console.log("\n=== SIMULATION REPORT ===");
    console.log(report.slice(0, 1000));
  })
  .catch(console.error);
