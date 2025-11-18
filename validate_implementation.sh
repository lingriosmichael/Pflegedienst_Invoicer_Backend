#!/bin/bash
# Phase 1 & 2 Implementation Validation Script
# Run this to verify all changes are working correctly

echo "🧪 Validating Phase 1 & 2 Implementation..."
echo ""

# Color codes
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

passed=0
failed=0

# Helper function
test_import() {
    local module=$1
    local description=$2
    
    if python3 -c "import sys; sys.path.insert(0, '.'); from $module" 2>/dev/null; then
        echo -e "${GREEN}✓${NC} $description"
        ((passed++))
    else
        echo -e "${RED}✗${NC} $description"
        ((failed++))
    fi
}

test_code() {
    local code=$1
    local description=$2
    
    if python3 -c "import sys; sys.path.insert(0, '.'); $code" 2>/dev/null; then
        echo -e "${GREEN}✓${NC} $description"
        ((passed++))
    else
        echo -e "${RED}✗${NC} $description"
        ((failed++))
    fi
}

echo "📦 Testing Imports..."
test_import "app.db.connection" "Database connection module"
test_import "app.exceptions" "Exception classes"
test_import "app.schemas.invoice" "Invoice schemas (Pydantic)"
test_import "app.utils.retry" "Retry decorator"
test_import "app.utils.parsing" "German decimal parser"
test_import "app.utils.patterns" "Regex pattern registry"
test_import "app.database" "Main database module"
test_import "app.pdf_parser" "PDF parser module"
test_import "app.backend" "FastAPI backend"

echo ""
echo "🔍 Testing Core Functionality..."

test_code "
from app.utils.parsing import GermanDecimalParser
assert GermanDecimalParser.parse('1.234,56') == 1234.56
assert GermanDecimalParser.parse('9,54') == 9.54
assert GermanDecimalParser.parse(123.45) == 123.45
" "GermanDecimalParser.parse() works with German format"

test_code "
from app.utils.parsing import GermanDecimalParser
assert GermanDecimalParser.to_german_string(1234.56) == '1.234,56'
assert GermanDecimalParser.to_german_string(9.54) == '9,54'
" "GermanDecimalParser.to_german_string() works"

test_code "
from app.utils.patterns import InvoicePatterns
assert InvoicePatterns.EURO_AMOUNT.value.match('1.234,56')
assert InvoicePatterns.CARE_PERIOD.value.search('Pflegezeitraum: 01.01.25 - 31.01.25')
" "Regex patterns are compiled and working"

test_code "
from app.exceptions import ValidationError, PatientNotFoundError
try:
    raise ValidationError('test', 'Test error')
except ValidationError as e:
    assert 'test' in str(e)
" "Custom exceptions work correctly"

test_code "
from app.utils.retry import retry_with_backoff
@retry_with_backoff(max_retries=1)
def test_fn():
    return 'success'
assert test_fn() == 'success'
" "Retry decorator works"

test_code "
from app.schemas.invoice import PatientSchema
p = PatientSchema(
    name='Müller, Karl',
    birthdate='06.01.1941',
    insurance_number='L011897478'
)
assert p.name == 'Müller, Karl'
" "Pydantic schemas validate correctly"

test_code "
from app.db.connection import get_db
# Test that context manager can be imported and used
with get_db() as conn:
    cursor = conn.cursor()
    cursor.execute('SELECT 1')
    assert cursor.fetchone()[0] == 1
" "Database context manager works"

test_code "
from app.database import init_db
init_db()
# If we get here without exception, it worked
assert True
" "Database initialization completes without errors"

echo ""
echo "📊 Summary:"
echo -e "${GREEN}Passed: $passed${NC}"
echo -e "${RED}Failed: $failed${NC}"

if [ $failed -eq 0 ]; then
    echo -e "\n${GREEN}✨ All validations passed! Implementation is complete.${NC}"
    exit 0
else
    echo -e "\n${RED}❌ Some validations failed. Check errors above.${NC}"
    exit 1
fi
