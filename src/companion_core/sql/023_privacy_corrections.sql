CREATE TABLE privacy_corrections (
 resource text NOT NULL, fingerprint text NOT NULL, level text NOT NULL, PRIMARY KEY(resource,fingerprint)
);
