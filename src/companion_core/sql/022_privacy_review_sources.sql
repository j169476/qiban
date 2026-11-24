CREATE TABLE privacy_review_sources (
 resource text NOT NULL, context_source text NOT NULL, PRIMARY KEY(resource,context_source)
);
CREATE INDEX privacy_review_sources_context ON privacy_review_sources(context_source);
