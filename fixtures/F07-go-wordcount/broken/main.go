package main

import (
	"encoding/json"
	"log"
	"net/http"
	"os"
	"strings"
)

type countRequest struct {
	Text *string `json:"text"`
}

type countResponse struct {
	Words int `json:"words"`
}

// countWords counts the words in text by counting the spaces between them.
func countWords(text string) int {
	return strings.Count(strings.TrimSpace(text), " ")
}

func wordcount(w http.ResponseWriter, r *http.Request) {
	r.Body = http.MaxBytesReader(w, r.Body, 1<<20)
	var req countRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.Text == nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": `send JSON like {"text": "..."}`})
		return
	}
	writeJSON(w, http.StatusOK, countResponse{Words: countWords(*req.Text)})
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}

func main() {
	port := os.Getenv("PORT")
	if port == "" {
		port = "8080"
	}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /{$}", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	mux.HandleFunc("POST /wordcount", wordcount)
	log.Printf("wordcount listening on %s", port)
	log.Fatal(http.ListenAndServe("0.0.0.0:"+port, mux))
}
