// MiroFish Go Client Reference
// ==============================
// Shows how to call the MiroFish REST API from a Go service.
// This is a reference implementation — copy and adapt as needed.
//
// Prerequisites:
//   - MiroFish backend running: cd backend && python run.py
//   - Place your cart seed doc at /tmp/cart_seed.txt before running
//
// Run: go run example_go_client.go

package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"os"
	"path/filepath"
	"time"
)

const miroFishURL = "http://localhost:5001"

// ---------------------------------------------------------------------------
// Response types
// ---------------------------------------------------------------------------

type Project struct {
	ProjectID string `json:"project_id"`
	Status    string `json:"status"`
	GraphID   string `json:"graph_id"`
	Error     string `json:"error"`
}

type Task struct {
	TaskID   string `json:"task_id"`
	Status   string `json:"status"`
	Progress int    `json:"progress"`
	Error    string `json:"error"`
}

type Simulation struct {
	SimulationID string `json:"simulation_id"`
	Status       string `json:"status"`
	Error        string `json:"error"`
}

type RunStatus struct {
	SimulationID    string  `json:"simulation_id"`
	RunnerStatus    string  `json:"runner_status"`
	ProgressPercent float64 `json:"progress_percent"`
	CurrentRound    int     `json:"current_round"`
	TotalRounds     int     `json:"total_rounds"`
}

type Report struct {
	Content string `json:"content"`
	Status  string `json:"status"`
}

// ---------------------------------------------------------------------------
// HTTP helpers
// ---------------------------------------------------------------------------

func getJSON(path string, out interface{}) error {
	resp, err := http.Get(miroFishURL + "/api" + path)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		return fmt.Errorf("API error %d: %s", resp.StatusCode, body)
	}
	return json.Unmarshal(body, out)
}

func postJSON(path string, payload interface{}, out interface{}) error {
	b, _ := json.Marshal(payload)
	resp, err := http.Post(miroFishURL+"/api"+path, "application/json", bytes.NewReader(b))
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		return fmt.Errorf("API error %d: %s", resp.StatusCode, body)
	}
	return json.Unmarshal(body, out)
}

func uploadFile(path string, filePath string, requirement string, out interface{}) error {
	f, err := os.Open(filePath)
	if err != nil {
		return err
	}
	defer f.Close()

	var buf bytes.Buffer
	w := multipart.NewWriter(&buf)
	fw, _ := w.CreateFormFile("files", filepath.Base(filePath))
	io.Copy(fw, f)
	w.WriteField("requirement", requirement)
	w.Close()

	resp, err := http.Post(miroFishURL+"/api"+path, w.FormDataContentType(), &buf)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		return fmt.Errorf("API error %d: %s", resp.StatusCode, body)
	}
	return json.Unmarshal(body, out)
}

// ---------------------------------------------------------------------------
// Pipeline steps
// ---------------------------------------------------------------------------

func pollTask(taskID string, intervalSec int, timeoutSec int) (*Task, error) {
	deadline := time.Now().Add(time.Duration(timeoutSec) * time.Second)
	for {
		var result struct {
			Task Task `json:"task"`
		}
		if err := getJSON("/graph/status/"+taskID, &result); err != nil {
			return nil, err
		}
		t := result.Task
		fmt.Printf("  task %s: status=%s progress=%d%%\n", taskID, t.Status, t.Progress)
		if t.Status == "COMPLETED" || t.Status == "FAILED" {
			return &t, nil
		}
		if time.Now().After(deadline) {
			return nil, fmt.Errorf("timeout waiting for task %s", taskID)
		}
		time.Sleep(time.Duration(intervalSec) * time.Second)
	}
}

func pollSimulation(simID string, intervalSec int, timeoutSec int) (*RunStatus, error) {
	deadline := time.Now().Add(time.Duration(timeoutSec) * time.Second)
	for {
		var status RunStatus
		if err := getJSON("/simulation/"+simID+"/run_status", &status); err != nil {
			return nil, err
		}
		fmt.Printf("  simulation: status=%s round=%d/%d (%.0f%%)\n",
			status.RunnerStatus, status.CurrentRound, status.TotalRounds, status.ProgressPercent)
		if status.RunnerStatus == "completed" || status.RunnerStatus == "failed" || status.RunnerStatus == "stopped" {
			return &status, nil
		}
		if time.Now().After(deadline) {
			return nil, fmt.Errorf("timeout waiting for simulation %s", simID)
		}
		time.Sleep(time.Duration(intervalSec) * time.Second)
	}
}

// ---------------------------------------------------------------------------
// Main: full cart recovery pipeline
// ---------------------------------------------------------------------------

func main() {
	seedFile := "/tmp/cart_seed.txt"
	requirement := "Simulate the psychology of a customer who abandoned their cart. " +
		"Predict abandonment reason and the best messaging angle for a recovery email."

	// Step 1: Upload + generate ontology
	fmt.Println("Step 1: Generating ontology...")
	var ontologyResult map[string]interface{}
	if err := uploadFile("/graph/ontology/generate", seedFile, requirement, &ontologyResult); err != nil {
		panic(err)
	}
	projectData, _ := json.Marshal(ontologyResult["project"])
	var project Project
	json.Unmarshal(projectData, &project)
	fmt.Printf("  project_id: %s\n", project.ProjectID)

	// Step 2: Build graph
	fmt.Println("Step 2: Building knowledge graph...")
	var buildResult map[string]interface{}
	if err := postJSON("/graph/build/"+project.ProjectID, nil, &buildResult); err != nil {
		panic(err)
	}
	taskID := fmt.Sprintf("%v", buildResult["task_id"])
	task, err := pollTask(taskID, 3, 180)
	if err != nil || task.Status == "FAILED" {
		panic(fmt.Sprintf("Graph build failed: %v", err))
	}

	// Step 3: Create simulation
	fmt.Println("Step 3: Creating simulation...")
	var simResult map[string]interface{}
	postJSON("/simulation/create", map[string]interface{}{
		"project_id":             project.ProjectID,
		"simulation_requirement": requirement,
		"enable_twitter":         true,
		"enable_reddit":          false,
	}, &simResult)
	simData, _ := json.Marshal(simResult["simulation"])
	var simulation Simulation
	json.Unmarshal(simData, &simulation)
	fmt.Printf("  simulation_id: %s\n", simulation.SimulationID)

	// Step 4: Prepare simulation
	fmt.Println("Step 4: Preparing simulation (generating agent profiles)...")
	postJSON("/simulation/"+simulation.SimulationID+"/prepare", nil, &map[string]interface{}{})

	deadline := time.Now().Add(20 * time.Minute)
	for {
		var prepStatus map[string]interface{}
		getJSON("/simulation/"+simulation.SimulationID+"/prepare_status", &prepStatus)
		status := fmt.Sprintf("%v", prepStatus["status"])
		fmt.Printf("  prepare status: %s\n", status)
		if status == "READY" || status == "FAILED" {
			break
		}
		if time.Now().After(deadline) {
			panic("timeout waiting for simulation preparation")
		}
		time.Sleep(10 * time.Second)
	}

	// Step 5: Start simulation
	fmt.Println("Step 5: Starting simulation...")
	postJSON("/simulation/"+simulation.SimulationID+"/start", nil, &map[string]interface{}{})

	runStatus, err := pollSimulation(simulation.SimulationID, 30, 3600)
	if err != nil {
		panic(err)
	}
	fmt.Printf("Simulation done: %s\n", runStatus.RunnerStatus)

	// Step 6: Generate report
	fmt.Println("Step 6: Generating report...")
	postJSON("/report/"+simulation.SimulationID+"/generate", nil, &map[string]interface{}{})

	for {
		var reportStatus map[string]interface{}
		getJSON("/report/"+simulation.SimulationID+"/status", &reportStatus)
		status := fmt.Sprintf("%v", reportStatus["status"])
		fmt.Printf("  report status: %s\n", status)
		if status == "completed" || status == "failed" {
			break
		}
		time.Sleep(5 * time.Second)
	}

	// Step 7: Get report
	var fullReport map[string]interface{}
	getJSON("/report/"+simulation.SimulationID+"/full", &fullReport)
	content := fmt.Sprintf("%v", fullReport["content"])

	fmt.Println("\n=== SIMULATION REPORT (first 500 chars) ===")
	if len(content) > 500 {
		fmt.Println(content[:500] + "...")
	} else {
		fmt.Println(content)
	}
}
