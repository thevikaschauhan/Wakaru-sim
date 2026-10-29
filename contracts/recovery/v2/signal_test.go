package recoveryv2

import (
	"encoding/json"
	"os"
	"strings"
	"testing"
)

func TestSignalConformance(t *testing.T) {
	raw, e := os.ReadFile("conformance.json")
	if e != nil {
		t.Fatal(e)
	}
	var fixtures []struct {
		Name   string          `json:"name"`
		Schema string          `json:"schema"`
		Valid  bool            `json:"valid"`
		Value  json.RawMessage `json:"value"`
	}
	if e = json.Unmarshal(raw, &fixtures); e != nil {
		t.Fatal(e)
	}
	for _, f := range fixtures {
		if f.Schema != "signal.schema.json" {
			continue
		}
		t.Run(f.Name, func(t *testing.T) {
			_, e := DecodeSignal(f.Value)
			if (e == nil) != f.Valid {
				t.Fatalf("valid=%v error=%v", f.Valid, e)
			}
		})
	}
	for _, f := range fixtures {
		if f.Name == "signal" {
			for _, suffix := range []string{`,"kind":"open"}`, `,"reason":null}`, `,"execute_send":true}`} {
				bad := strings.TrimSuffix(strings.TrimSpace(string(f.Value)), "}") + suffix
				if _, e := DecodeSignal([]byte(bad)); e == nil {
					t.Fatal("accepted invalid JSON", suffix)
				}
			}
		}
	}
}
