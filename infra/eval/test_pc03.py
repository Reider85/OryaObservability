#!/usr/bin/env python3
"""
Test script to verify PC03 implementation
"""
import os
import yaml
import subprocess

def test_docker_compose_config():
    """Test that docker-compose.yml has correct eval services"""
    print("Testing docker-compose.yml configuration...")
    
    with open('C:/projects/OryaObservability/infra/docker-compose.yml', 'r') as f:
        config = yaml.safe_load(f)
    
    # Check eval-redis service
    assert 'eval-redis' in config['services'], "eval-redis service missing"
    redis_service = config['services']['eval-redis']
    assert redis_service['image'] == 'redis:7', "eval-redis should use redis:7"
    assert '--database 1' in redis_service['command'], "eval-redis should use database 1"
    
    # Check eval-worker service
    assert 'eval-worker' in config['services'], "eval-worker service missing"
    worker_service = config['services']['eval-worker']
    assert worker_service['image'] == 'eval-worker:latest', "eval-worker should use custom image"
    assert worker_service['scale'] == 2, "eval-worker should scale to 2"
    assert 'EVAL_REDIS_URL' in worker_service['environment'], "EVAL_REDIS_URL missing"
    assert 'EVAL_QUEUE_NAME' in worker_service['environment'], "EVAL_QUEUE_NAME missing"
    
    # Check eval_redis_data volume
    assert 'eval_redis_data' in config['volumes'], "eval_redis_data volume missing"
    
    print("OK: docker-compose.yml configuration is correct")

def test_env_example():
    """Test that .env.example has all required variables"""
    print("Testing .env.example configuration...")
    
    required_vars = [
        'EVAL_REDIS_URL',
        'EVAL_QUEUE_NAME', 
        'EVAL_WORKER_CONCURRENCY',
        'LLM_JUDGE_MODEL',
        'LLM_JUDGE_API_KEY',
        'EMBEDDING_MODEL',
        'OPENAI_API_KEY'
    ]
    
    with open('C:/projects/OryaObservability/infra/.env.example', 'r') as f:
        content = f.read()
    
    for var in required_vars:
        assert var in content, f"{var} missing from .env.example"
    
    print("OK: .env.example has all required variables")

def test_worker_dockerfile():
    """Test that worker.Dockerfile has all required dependencies"""
    print("Testing worker.Dockerfile...")
    
    required_packages = [
        'rq',
        'httpx',
        'presidio-analyzer',
        'transformers',
        'clickhouse-driver',
        'psycopg',
        'boto3',
        'pyarrow'
    ]
    
    with open('C:/projects/OryaObservability/infra/eval/worker.Dockerfile', 'r') as f:
        content = f.read()
    
    for package in required_packages:
        assert package in content, f"{package} missing from worker.Dockerfile"
    
    print("OK: worker.Dockerfile has all required dependencies")

def test_supervisord_config():
    """Test that supervisord.conf has correct configuration"""
    print("Testing supervisord.conf...")
    
    with open('C:/projects/OryaObservability/infra/eval/supervisord.conf', 'r') as f:
        content = f.read()
    
    assert '[rqevalworker]' in content, "rqevalworker section missing"
    assert 'autorestart=true' in content, "autorestart should be enabled"
    assert 'startretries=3' in content, "startretries should be 3"
    assert '/dev/stdout' in content, "stdout logging should be enabled"
    
    print("OK: supervisord.conf has correct configuration")

def test_readme():
    """Test that README.md has all required sections"""
    print("Testing README.md...")
    
    with open('C:/projects/OryaObservability/infra/eval/README.md', 'r') as f:
        content = f.read()
    
    required_sections = [
        '## Services',
        '## Environment Variables', 
        '## Usage',
        '## Debugging',
        '## Dependencies'
    ]
    
    for section in required_sections:
        assert section in content, f"{section} missing from README.md"
    
    print("OK: README.md has all required sections")

def main():
    """Run all tests"""
    print("Running PC03 implementation tests...\n")
    
    try:
        test_docker_compose_config()
        test_env_example()
        test_worker_dockerfile()
        test_supervisord_config()
        test_readme()
        
        print("\nAll tests passed! PC03 implementation is complete.")
        print("\nDoD Checklist:")
        print("- [x] docker compose config is valid (services eval-redis and eval-worker present)")
        print("- [x] .env.example contains all required variables")
        print("- [x] eval-redis service uses database 1")
        print("- [x] eval-worker service scales to 2 replicas")
        print("- [x] All required dependencies are in worker.Dockerfile")
        print("- [x] supervisord.conf has correct restart policy")
        print("- [x] README.md has usage instructions")
        
    except Exception as e:
        print(f"\nTest failed: {e}")
        return 1
    
    return 0

if __name__ == "__main__":
    exit(main())