CREATE TABLE presence_reports (
    user_id BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    observed_ns BIGINT NOT NULL,
    projected_ns BIGINT NOT NULL,
    reporter TEXT,
    sequence BIGINT,
    location TEXT
);
