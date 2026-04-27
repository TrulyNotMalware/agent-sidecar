// Minimal Claude Sidecar client (Go).
//
// Streams SSE events from /v1/converse and prints them. Demonstrates the
// HTTP+SSE contract — the sidecar's whole reason for being.
//
// Run:
//
//	BEARER_SECRET=... go run client.go "what's the weather?"
package main

import (
	"bufio"
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"strings"
)

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "usage: client <prompt>")
		os.Exit(1)
	}
	url := envOr("SIDECAR_URL", "http://127.0.0.1:7300") + "/v1/converse"
	bearer := os.Getenv("BEARER_SECRET")
	if bearer == "" {
		fmt.Fprintln(os.Stderr, "BEARER_SECRET unset")
		os.Exit(1)
	}

	body, _ := json.Marshal(map[string]any{
		"sessionKey": "demo:go",
		"prompt":     os.Args[1],
	})
	req, _ := http.NewRequest("POST", url, bytes.NewReader(body))
	req.Header.Set("Authorization", "Bearer "+bearer)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "text/event-stream")

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		panic(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		panic(fmt.Sprintf("sidecar returned %d", resp.StatusCode))
	}

	scanner := bufio.NewScanner(resp.Body)
	scanner.Buffer(make([]byte, 0, 64*1024), 1024*1024)

	var event string
	for scanner.Scan() {
		line := scanner.Text()
		switch {
		case line == "":
			event = ""
		case strings.HasPrefix(line, "event: "):
			event = strings.TrimPrefix(line, "event: ")
		case strings.HasPrefix(line, "data: "):
			fmt.Printf("[%s] %s\n", event, strings.TrimPrefix(line, "data: "))
		}
	}
	if err := scanner.Err(); err != nil {
		panic(err)
	}
}
