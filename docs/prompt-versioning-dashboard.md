# System Prompt Versioning Dashboard

This dashboard provides visibility into system prompt usage, versioning, and changes across your LLM infrastructure. It enables A/B testing analysis, prompt version tracking, and helps identify when prompt changes might impact system behavior.

## Overview

The dashboard includes several key metrics:

1. **Unique System Prompt Templates** - Number of distinct system prompts used over time
2. **System Prompt Version Distribution** - Pie chart showing the distribution of different prompt versions
3. **Traces by System Prompt Template** - Bar chart showing which prompts are used most frequently
4. **New Prompt Versions** - Count of new prompt versions detected in the selected time range
5. **Top System Prompt Templates** - Most frequently used prompt templates with their version labels

## Key Features

### Prompt Version Tracking
- Each system prompt is hashed using SHA256 to create a unique identifier
- Version labels are automatically extracted from prompts (looking for `VERSION: x.y.z` patterns)
- Changes in prompt versions are tracked and can trigger alerts

### A/B Testing Support
- The dashboard helps identify when prompt changes are introduced
- You can correlate prompt version changes with performance metrics
- Supports analysis of which prompt versions are used most frequently

### Traceability
- Each trace that uses a system prompt is linked to the specific prompt version
- You can explore all traces that used a particular prompt template
- Full audit trail of prompt usage over time

## Usage

### Monitoring Prompt Changes
1. Watch the "New Prompt Versions" panel for sudden increases
2. Check the "System Prompt Version Distribution" to see if new versions are being adopted
3. Use the "Traces by System Prompt Template" to understand usage patterns

### A/B Testing Analysis
1. Identify the time period when a prompt change was introduced
2. Correlate with your application metrics (performance, user satisfaction, etc.)
3. Compare the usage patterns of different prompt versions

### Troubleshooting
1. If you observe unexpected behavior, check if any prompt changes occurred
2. Use the "Top System Prompt Templates" to see which prompts are most active
3. Explore traces with specific prompts to understand context

## Alerts

### Prompt Version Change Alert
```sql
-- Alert when more than 5 new prompt versions are detected in 1 hour
SELECT count(DISTINCT version_label) 
FROM prompt_templates 
WHERE time >= now() - INTERVAL 1 HOUR 
HAVING count(DISTINCT version_label) > 5
```

### Prompt Adoption Alert
```sql
-- Alert when a new prompt version is used by more than 10% of traces
SELECT version_label, count(DISTINCT trace_id) as trace_count
FROM spans_hot 
WHERE time >= now() - INTERVAL 1 HOUR 
  AND attributes['llm.system_prompt_sha256'] IS NOT NULL
GROUP BY version_label
HAVING trace_count > (SELECT count(DISTINCT trace_id) FROM spans_hot WHERE time >= now() - INTERVAL 1 HOUR) * 0.1
```

## Integration with Existing Systems

### ClickHouse Tables
- `prompt_templates` - Stores prompt template mappings with SHA256 hashes
- `spans_hot` - Contains spans with `llm.system_prompt_sha256` attributes
- `prompt_templates_changes` - Tracks version changes over time

### Trace Viewer Integration
The trace viewer now shows:
- System prompt version label (if available)
- SHA256 hash of the system prompt
- Link to view all traces with the same prompt template

### API Endpoints
- `/api/prompts` - List available prompt templates
- `/api/prompts/{sha256}/traces` - Get traces using a specific prompt
- `/api/prompts/changes` - Get prompt version changes over time

## Best Practices

### Prompt Versioning
1. Use semantic versioning (e.g., `VERSION: 1.2.3`) in your prompts
2. Include meaningful version labels that describe the changes
3. Test new prompt versions thoroughly before deployment

### Monitoring
1. Set up alerts for unusual prompt changes
2. Monitor the adoption rate of new prompt versions
3. Correlate prompt changes with system performance metrics

### Analysis
1. Use the dashboard to understand prompt usage patterns
2. Conduct A/B testing to compare prompt effectiveness
3. Maintain an audit trail of all prompt changes

## Troubleshooting

### Common Issues

**Missing Prompt Versions**
- Check if prompts include version labels
- Verify that system prompts are being extracted correctly
- Ensure the content sampling rate includes the traces you're interested in

**High Cardinality**
- Many unique prompt versions can make the dashboard hard to read
- Consider normalizing similar prompts or using prompt categories
- Use the SHA256 filter to focus on specific prompts

**Performance Issues**
- Large volumes of prompt data can impact query performance
- Consider increasing the TTL or archiving old prompt data
- Use the time range filters to limit the data volume

### Debug Queries

```sql
-- Check recent prompt versions
SELECT sha256, version_label, first_seen, last_seen
FROM prompt_templates 
ORDER BY first_seen DESC 
LIMIT 10

-- Find traces with a specific prompt
SELECT trace_id, span_id, start_time
FROM spans_hot 
WHERE attributes['llm.system_prompt_sha256'] = 'your-sha256-here'
ORDER BY start_time DESC 
LIMIT 10

-- Check prompt version changes over time
SELECT version_label, count() as usage_count
FROM prompt_templates 
GROUP BY version_label 
ORDER BY usage_count DESC
```

## Implementation Notes

The system prompt versioning feature was implemented as part of PP53 (Prompt-template versioning) and integrates with:

- **ObsPipeline**: Enriches spans with prompt information
- **ClickHouse**: Stores prompt template mappings with 90-day TTL
- **Content Sampling**: Only stores prompt templates for sampled traces
- **Trace Viewer**: Displays prompt version information

The feature is designed to be non-intrusive and only adds minimal overhead to the proxy pipeline.