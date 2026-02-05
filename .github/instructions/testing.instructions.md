---
description: 'Testing standards for pytest-based test suites, focusing on clarity, maintainability, and alignment with self-documenting code principles.'
applyTo: 'tests/**/*.py'
---

# Testing Standards

## Core Philosophy

**Tests are documentation.** Write tests that clearly communicate intent through structure and naming, not verbose comments. Follow the same minimalist principles as production code.

## Test Function Naming

### Standard Pattern

Use descriptive names that communicate: **what is being tested**, **the scenario**, and **expected outcome**.

```python
# Good: Clear what's tested, scenario, and outcome
def test_install_packages_updates_apt_cache_before_installation():
    ...

def test_install_packages_raises_when_apt_update_fails():
    ...

def test_start_service_restarts_nginx():
    ...

# Bad: Vague, generic naming
def test_install():
    ...

def test_start():
    ...

def test_success():
    ...
```

### Naming Conventions

- Use underscores to separate logical parts: `test_<function>_<scenario>_<outcome>`
- Be specific about the scenario being tested
- Avoid generic suffixes like `_success` or `_failure` unless combined with specifics
- For parametrized tests testing multiple scenarios, use descriptive parameter names

```python
# Good: Specific scenarios
def test_install_creates_srv_directories_with_correct_permissions():
    ...

def test_setup_systemd_unit_injects_proxy_environment_variables():
    ...

# Avoid: Generic suffixes without context
def test_install_success():
    ...

def test_setup_success_and_failure():  # Tests two things - split them
    ...
```

## Test Structure: AAA Pattern

**Always use Arrange-Act-Assert structure** to make tests scannable and predictable. Use blank lines to visually separate the three sections.

```python
def test_refresh_report_starts_all_static_report_services(monkeypatch):
    starts = []
    monkeypatch.setattr(staticreports.systemd, "service_start", lambda *args: starts.append(args))
    sr = staticreports.StaticReports()
    
    sr.refresh_report()
    
    for svc in staticreports.UBUNTU_STATIC_REPORT_SERVICES:
        assert any(call for call in starts if svc + ".service" in call)
```

### Structure Guidelines

- **Arrange**: Set up mocks, create test objects, prepare test data
- **Act**: Execute the function/method being tested (typically ONE action)
- **Assert**: Verify expected behavior (can be multiple assertions related to the same action)
- Blank lines separate sections - no AAA comments needed
- Keep arrangement simple; extract complex setup to fixtures or helper functions

**Use docstrings sparingly** - only when the test scenario is non-obvious or requires business context.

```python
# Good: No docstring needed - test name is self-explanatory
def test_install_copies_update_sync_blocklist_script_to_usr_bin(monkeypatch):
    ...

# Good: Docstring adds value - explains WHY this edge case matters
def test_get_external_url_falls_back_to_fqdn_when_ingress_unavailable(ctx):
    """When ingress relation is not available, the charm must use the unit's
    FQDN to construct the external URL for Launchpad callback registration.
    This ensures the service remains functional in non-Kubernetes deployments.
    """
    ...

# Bad: Docstring repeats the test name
def test_start_service_restarts_nginx():
    """Test that the start service restarts nginx."""
    ...
```

### When to Use Docstrings

- Complex business logic or edge cases that aren't obvious from the name
- Integration test scenarios involving multiple components
- Regression tests for specific bugs (include issue reference)
- Tests with non-obvious setup or teardown requirements

## Mock Strategy

### Prefer Monkeypatch for Simple Cases

Use `monkeypatch` for straightforward replacements where you control the setup:

```python
def test_install_packages_calls_apt_update_before_adding_packages(monkeypatch):
    called = []
    monkeypatch.setattr(staticreports.apt, "update", lambda: called.append("update"))
    monkeypatch.setattr(staticreports.apt, "add_package", lambda pkg: called.append(pkg))
    sr = staticreports.StaticReports()

    sr._install_packages()

    assert called[0] == "update"
```

### Use @patch for Complex Scenarios

Use `@patch` decorator when:
- Mocking properties (use `new_callable=PropertyMock`)
- Need to verify call counts with `.assert_called_once()`
- Multiple tests share the same mock setup
- Testing exception handling

```python
@patch("charm.StaticReports.install")
@patch("charm.StaticReports.setup_systemd_units")
def test_install_event_sets_active_status_on_success(systemd_mock, install_mock, ctx, base_state):
    install_mock.return_value = True
    systemd_mock.return_value = True
    
    out = ctx.run(ctx.on.install(), base_state)
    
    assert out.unit_status == ActiveStatus()
    assert install_mock.called
```

### Mock Naming Conventions

- Suffix `@patch` mocks with `_mock`: `install_mock`, `configure_mock`
- Keep monkeypatch lambdas inline when simple (1 line)
- Extract complex monkeypatch functions with descriptive names: `fake_read_text`, `fake_write_text`

## Parametrized Tests

Use `@pytest.mark.parametrize` for testing multiple inputs, but keep parameter names descriptive:

```python
# Good: Descriptive parameter names
@pytest.mark.parametrize(
    "exception_type,expected_status",
    [
        (PackageError, BlockedStatus("...")),
        (PackageNotFoundError, BlockedStatus("...")),
        (CalledProcessError(1, "apt"), BlockedStatus("...")),
    ],
)
def test_install_event_blocks_charm_when_package_installation_fails(
    exception_type, expected_status, ctx, base_state
):
    ...

# Avoid: Single generic parameter name for multiple scenarios
@pytest.mark.parametrize("exception", [PackageError, PackageNotFoundError])
def test_install_failure(mock, exception, ctx, base_state):
    ...
```

## Assertion Guidelines

### Be Explicit

```python
# Good: Explicit about what's being checked
assert out.unit_status == ActiveStatus()
assert install_mock.called
assert ("copy", "src/script/update-sync-blocklist", "/usr/bin") in ops

# Avoid: Vague or implicit checks
assert result  # What does True mean here?
assert len(calls) == 3  # What are the 3 calls?
```

### Group Related Assertions

```python
# Good: Related assertions together
def test_setup_systemd_unit_writes_service_and_timer_files(monkeypatch):
    ...
    
    assert svc_path in written
    assert timer_path in written
    assert "Environment=HTTP_PROXY=http://proxy.example:8080" in written[svc_path]
    assert "Environment=HTTPS_PROXY=https://secure.example:8443" in written[svc_path]
```

### One Logical Concept Per Test

If you find yourself writing many unrelated assertions, split into separate tests:

```python
# Avoid: Testing too many things
def test_start_success_and_failure(monkeypatch):
    # Tests both success case and failure case - should be two tests
    ...

# Good: Split into focused tests
def test_start_restarts_nginx_and_starts_report_services(monkeypatch):
    ...

def test_start_raises_when_systemd_service_start_fails(monkeypatch):
    ...
```

## Test Organization

### File Structure

- Mirror source structure: `src/charm.py` → `tests/unit/test_charm.py`
- Group related tests together (e.g., all `install` event tests)
- Use fixtures for common setup (context, state)

### Fixtures

```python
# Good: Reusable, well-named fixtures
@pytest.fixture
def ctx():
    """Scenario test context for charm testing."""
    return Context(UbuntuStaticReportsCharm)

@pytest.fixture
def base_state():
    """Minimal state for charm testing with leadership."""
    return State(leader=True)
```

### Test Independence

- Each test must run independently
- Use fixtures or setup/teardown for shared state
- Avoid test interdependencies (test order shouldn't matter)
- Clean up any global state

## Anti-Patterns to Avoid

### Redundant Comments

```python
# Bad: Comment repeats test name
def test_install_success(systemd_mock, install_mock, ctx, base_state):
    # Install the charm  ← Redundant
    install_mock.return_value = True
    ...
```

### Testing Implementation Details

```python
# Avoid: Testing how something is done instead of what it does
def test_install_calls_apt_exactly_five_times():
    ...

# Good: Testing behavior and outcomes
def test_install_installs_all_required_packages():
    ...
```

### Overly Complex Test Setup

If your test requires extensive setup, consider:
- Extracting helper functions
- Using fixtures
- Simplifying the production code
- Whether you're testing too much in one test

### Magic Values

```python
# Avoid: Unclear magic values
assert len(calls) == 5
assert result[2] == "foo"

# Good: Named constants or clear references
assert len(calls) == len(PACKAGES)
assert "update-sync-blocklist" in result
```

## Quality Checklist

Before committing tests:
- [ ] Test names clearly describe scenario and expected outcome
- [ ] AAA structure is evident (with blank lines separating sections)
- [ ] No redundant comments (test is self-documenting)
- [ ] Docstrings only on non-obvious scenarios
- [ ] Appropriate mock strategy (monkeypatch vs @patch)
- [ ] Explicit, meaningful assertions
- [ ] Test runs independently
- [ ] Test name follows `test_<function>_<scenario>_<outcome>` pattern

## Summary

**Tests should read like specifications.** A developer unfamiliar with the code should understand what's being tested, under what conditions, and what the expected behavior is—all from the test name and structure, without needing comments.
