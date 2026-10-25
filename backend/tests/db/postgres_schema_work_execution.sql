CREATE TABLE work_item_run (
	id BIGINT GENERATED ALWAYS AS IDENTITY,
	owner_id VARCHAR NOT NULL,
	work_item_id BIGINT NOT NULL,
	routine_id BIGINT NOT NULL,
	idempotency_key VARCHAR NOT NULL,
	attempt INTEGER NOT NULL,
	cancelled_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (work_item_id, idempotency_key),
	UNIQUE (work_item_id, attempt),
	UNIQUE (routine_id),
	FOREIGN KEY(work_item_id) REFERENCES work_item (id),
	FOREIGN KEY(routine_id) REFERENCES routine (id)
);

CREATE TABLE work_item_event (
	id BIGINT GENERATED ALWAYS AS IDENTITY,
	owner_id VARCHAR NOT NULL,
	work_item_id BIGINT NOT NULL,
	receipt_id BIGINT,
	key VARCHAR NOT NULL,
	kind VARCHAR NOT NULL,
	actor VARCHAR NOT NULL,
	detail TEXT,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (work_item_id, key),
	FOREIGN KEY(work_item_id) REFERENCES work_item (id),
	FOREIGN KEY(receipt_id) REFERENCES work_item_run (id)
);
