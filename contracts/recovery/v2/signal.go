// Package recoveryv2 contains the pinned cross-service AF01 wire contracts.
package recoveryv2

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"github.com/google/uuid"
	"io"
	"net/url"
	"strings"
	"time"
	"unicode/utf8"
)

type Signal struct {
	SchemaVersion      string    `json:"schema_version"`
	EventID            string    `json:"event_id"`
	MerchantID         string    `json:"merchant_id"`
	StoreID            int       `json:"store_id"`
	Source             string    `json:"source"`
	SourceAccountID    string    `json:"source_account_id"`
	SourceEventID      string    `json:"source_event_id"`
	OccurredAt         time.Time `json:"occurred_at"`
	ReceivedAt         time.Time `json:"received_at"`
	Kind               string    `json:"kind"`
	Provenance         string    `json:"provenance"`
	VerificationStatus string    `json:"verification_status"`
	JourneyID          string    `json:"journey_id,omitempty"`
	MessageID          string    `json:"message_id,omitempty"`
	DocumentID         string    `json:"document_id,omitempty"`
	AttemptID          string    `json:"attempt_id,omitempty"`
	ProviderMessageID  string    `json:"provider_message_id,omitempty"`
	EpisodeID          string    `json:"episode_id,omitempty"`
	SessionID          string    `json:"session_id,omitempty"`
	IdentityRevision   *int64    `json:"identity_revision,omitempty"`
	PayloadRef         string    `json:"payload_ref"`
	PayloadHash        string    `json:"payload_hash"`
	LinkID             string    `json:"link_id,omitempty"`
	DestinationURL     string    `json:"destination_url,omitempty"`
	AutomationClass    string    `json:"automation_class"`
	Reason             string    `json:"reason,omitempty"`
}

func Hash(b []byte) string { h := sha256.Sum256(b); return hex.EncodeToString(h[:]) }

// DecodeSignal rejects duplicate keys as well as unknown fields and trailing data.
func DecodeSignal(b []byte) (Signal, error) {
	var s Signal
	if err := CheckJSON(b); err != nil {
		return s, err
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(b, &fields); err != nil {
		return s, err
	}
	for k, v := range fields {
		if member(k, "journey_id", "message_id", "document_id", "attempt_id", "provider_message_id", "episode_id", "session_id", "link_id") {
			var value string
			if json.Unmarshal(v, &value) != nil || value == "" {
				return s, errors.New("empty optional identity")
			}
		}
		if string(v) == "null" {
			return s, errors.New("null signal field")
		}
		if k == "occurred_at" || k == "received_at" {
			var stamp string
			_ = json.Unmarshal(v, &stamp)
			if !strings.HasSuffix(stamp, "Z") {
				return s, errors.New("UTC timestamp required")
			}
		}
	}
	d := json.NewDecoder(bytes.NewReader(b))
	d.DisallowUnknownFields()
	if err := d.Decode(&s); err != nil {
		return s, err
	}
	if err := d.Decode(new(any)); err != io.EOF {
		return s, errors.New("trailing JSON")
	}
	return s, s.Validate()
}

// CheckJSON rejects duplicate keys and trailing values before normalization.
func CheckJSON(b []byte) error {
	if !utf8.Valid(b) {
		return errors.New("invalid UTF-8")
	}
	d := json.NewDecoder(bytes.NewReader(b))
	d.UseNumber()
	if err := uniqueKeys(d); err != nil {
		return err
	}
	if _, err := d.Token(); err != io.EOF {
		return errors.New("trailing JSON")
	}
	return nil
}
func uniqueKeys(d *json.Decoder) error {
	t, e := d.Token()
	if e != nil {
		return e
	}
	delim, ok := t.(json.Delim)
	if !ok {
		return nil
	}
	if delim == '{' {
		seen := map[string]bool{}
		for d.More() {
			k, e := d.Token()
			if e != nil {
				return e
			}
			key := k.(string)
			if seen[key] {
				return errors.New("duplicate JSON key")
			}
			seen[key] = true
			if e = uniqueKeys(d); e != nil {
				return e
			}
		}
	} else if delim == '[' {
		for d.More() {
			if e = uniqueKeys(d); e != nil {
				return e
			}
		}
	} else {
		return errors.New("unexpected delimiter")
	}
	_, e = d.Token()
	return e
}
func (s Signal) Validate() error {
	invalid := errors.New("invalid recovery_signal_v1")
	for _, v := range []string{s.SourceAccountID, s.SourceEventID, s.PayloadRef, s.ProviderMessageID, s.EpisodeID, s.SessionID, s.LinkID, s.DestinationURL, s.Reason} {
		if strings.ContainsRune(v, 0) {
			return invalid
		}
	}
	if s.SchemaVersion != "recovery_signal_v1" || s.StoreID < 1 || s.VerificationStatus != "verified" {
		return invalid
	}
	for _, id := range []string{s.EventID, s.MerchantID} {
		u, e := uuid.Parse(id)
		if e != nil || u == uuid.Nil || u.String() != id {
			return invalid
		}
	}
	for _, id := range []string{s.JourneyID, s.MessageID, s.DocumentID, s.AttemptID} {
		if id != "" {
			u, e := uuid.Parse(id)
			if e != nil || u == uuid.Nil || u.String() != id {
				return invalid
			}
		}
	}
	for _, x := range []string{s.SourceAccountID, s.SourceEventID, s.PayloadRef} {
		if len(x) < 1 || len(x) > 512 {
			return invalid
		}
	}
	for _, x := range []string{s.ProviderMessageID, s.EpisodeID, s.SessionID, s.LinkID} {
		if len(x) > 512 {
			return invalid
		}
	}
	b, e := hex.DecodeString(s.PayloadHash)
	if e != nil || len(b) != 32 || s.PayloadHash != strings.ToLower(s.PayloadHash) {
		return invalid
	}
	if s.OccurredAt.IsZero() || s.ReceivedAt.IsZero() || s.OccurredAt.Year() < 1 || s.OccurredAt.Year() > 9999 || s.ReceivedAt.Year() < 1 || s.ReceivedAt.Year() > 9999 {
		return invalid
	}
	if s.IdentityRevision != nil && *s.IdentityRevision < 0 {
		return invalid
	}
	if !member(s.Source, "sendgrid", "shopify", "pixel", "merchant") || !member(s.Provenance, "provider_event_id", "fallback_digest", "source_event_id") || !member(s.AutomationClass, "unknown", "machine_open", "suspected_automation", "human_verified") {
		return invalid
	}
	if !member(s.Kind, "delivered", "bounce", "open", "click", "unsubscribe", "group_unsubscribe", "spamreport", "dropped", "deferred", "processed", "purchase", "activity", "permission_change") {
		return invalid
	}
	if len(s.Reason) > 1024 || len(s.DestinationURL) > 2048 {
		return invalid
	}
	if s.DestinationURL != "" {
		u, e := url.Parse(s.DestinationURL)
		if e != nil || u.Host == "" || !member(u.Scheme, "http", "https") || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
			return invalid
		}
	}
	return nil
}
func member(s string, values ...string) bool {
	for _, v := range values {
		if s == v {
			return true
		}
	}
	return false
}
