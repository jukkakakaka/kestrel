CREATE TABLE fill_request (
    id      TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    result  TEXT NOT NULL
);

ALTER TABLE receipt ADD COLUMN request_id TEXT;
CREATE UNIQUE INDEX idx_receipt_request ON receipt(request_id);
