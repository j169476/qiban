CREATE TABLE privacy_dependencies (
 derived text NOT NULL, fragment text NOT NULL, revision text NOT NULL,
 PRIMARY KEY(derived,fragment,revision)
);
CREATE INDEX privacy_dependencies_fragment ON privacy_dependencies(fragment);
