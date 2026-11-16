-- Records what the agent did to produce each answer.
ALTER TABLE messages ADD COLUMN tool_trace TEXT;
