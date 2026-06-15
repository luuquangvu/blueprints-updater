"""Tests for blueprint text extraction logic."""

from custom_components.blueprints_updater.coordinator import BlueprintUpdateCoordinator


def test_extract_blueprint_text_standard():
    """Test extracting a standard blueprint block."""
    content = """blueprint:
  name: test
  description: A test blueprint
  domain: automation
  input:
    test_input:
      name: Test

trigger:
  - platform: state
    entity_id: sensor.test

action:
  - service: light.turn_on
"""
    extracted = BlueprintUpdateCoordinator._extract_blueprint_text(content)
    assert "trigger:" not in extracted
    assert "action:" not in extracted
    assert "blueprint:" in extracted
    assert "name: test" in extracted


def test_extract_blueprint_text_with_comments_and_blank_lines():
    """Test extracting blueprint block with comments and blank lines."""
    content = """
# Header comment
blueprint:
  name: test

  # Comment inside
  input: {}

# Comment before trigger
trigger:
  - platform: time
"""
    extracted = BlueprintUpdateCoordinator._extract_blueprint_text(content)
    assert "trigger:" not in extracted
    assert "# Comment before trigger" in extracted
    assert "input: {}" in extracted
    assert "# Header comment" not in extracted  # Skipped because it's before blueprint:


def test_extract_blueprint_text_no_blueprint_block():
    """Test fallback when no blueprint block is found."""
    content = """
automation:
  - alias: Test
    trigger: []
    action: []
"""
    extracted = BlueprintUpdateCoordinator._extract_blueprint_text(content)
    # Should fallback to returning the entire content
    assert extracted == content


def test_extract_blueprint_text_blueprint_at_end():
    """Test when blueprint block is the last thing in the file."""
    content = """blueprint:
  name: test
  domain: automation"""
    extracted = BlueprintUpdateCoordinator._extract_blueprint_text(content)
    assert extracted == content


def test_extract_blueprint_text_multiline_string():
    """Test blueprint block with multi-line strings."""
    content = """blueprint:
  name: test
  description: >
    This is a long
    multi-line description
    with no issues.
  domain: automation

action:
  - service: test
"""
    extracted = BlueprintUpdateCoordinator._extract_blueprint_text(content)
    assert "action:" not in extracted
    assert "multi-line description" in extracted


def test_extract_blueprint_text_weird_spacing():
    """Test fallback when blueprint: is not standard formatted."""
    content = """blueprint :
  name: test
action:
  - service: test
"""
    extracted = BlueprintUpdateCoordinator._extract_blueprint_text(content)
    # Because it starts with 'blueprint :', it won't match line.startswith("blueprint:")
    # It should fallback to full content
    assert extracted == content


def test_extract_blueprint_text_with_yaml_anchors():
    """Test fallback when YAML anchors are defined outside the blueprint block."""
    content = """
.anchors:
  - &my_anchor 123

blueprint:
  name: test
  custom_value: *my_anchor

action: []
"""
    # The text extractor will cut out the anchors
    extracted = BlueprintUpdateCoordinator._extract_blueprint_text(content)
    assert "*my_anchor" in extracted
    assert "&my_anchor" not in extracted

    # We test that _get_blueprint_block handles this by falling back to full parse
    bp = BlueprintUpdateCoordinator._get_blueprint_block("test_path", content)
    assert bp is not None
    assert bp.get("name") == "test"
    assert bp.get("custom_value") == 123
