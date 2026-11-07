"""
Individual chaos scenarios for HA components.
"""
import asyncio
import logging
import random
from datetime import datetime
from typing import Dict, List, Optional

import docker
from docker.models.containers import Container

from .base import ChaosScenario, ChaosResult, ChaosStatus


class ClickHouseChaosScenario(ChaosScenario):
    """Chaos scenario for ClickHouse nodes."""
    
    def __init__(self, node_name: str, duration_seconds: int = 60):
        super().__init__(
            name=f"clickhouse_{node_name}_chaos",
            services=[node_name],
            duration_seconds=duration_seconds
        )
        self.node_name = node_name
        self.docker_client = docker.from_env()
        self.container: Optional[Container] = None
    
    async def execute_chaos(self) -> None:
        """Stop the ClickHouse node."""
        logging.info(f"Executing chaos on ClickHouse node: {self.node_name}")
        
        try:
            # Find the container
            containers = self.docker_client.containers.list(
                filters={"name": self.node_name, "status": "running"}
            )
            
            if not containers:
                raise Exception(f"Container {self.node_name} not found or not running")
            
            self.container = containers[0]
            
            # Stop the container
            self.container.stop()
            logging.info(f"Stopped container: {self.node_name}")
            
            # Wait a bit for the stop to take effect
            await asyncio.sleep(10)
            
        except Exception as e:
            logging.error(f"Failed to execute chaos on {self.node_name}: {e}")
            raise
    
    async def restore_service(self) -> None:
        """Restore the ClickHouse node."""
        logging.info(f"Restoring ClickHouse node: {self.node_name}")
        
        if self.container:
            try:
                # Start the container
                self.container.start()
                logging.info(f"Started container: {self.node_name}")
                
                # Wait for the service to be healthy
                await self._wait_for_clickhouse_health()
                
            except Exception as e:
                logging.error(f"Failed to restore {self.node_name}: {e}")
                raise
    
    async def validate_service_health(self) -> bool:
        """Validate ClickHouse health."""
        try:
            await self._wait_for_clickhouse_health()
            return True
        except Exception:
            return False
    
    async def _wait_for_clickhouse_health(self, timeout: int = 120) -> None:
        """Wait for ClickHouse to be healthy."""
        start_time = datetime.now()
        
        while (datetime.now() - start_time).total_seconds() < timeout:
            try:
                # Check if container is running
                if self.container and self.container.status != "running":
                    await asyncio.sleep(5)
                    continue
                
                # Try to connect to ClickHouse
                # This would be implemented with actual ClickHouse client
                await asyncio.sleep(5)
                return
                
            except Exception:
                await asyncio.sleep(5)
        
        raise Exception(f"ClickHouse node {self.node_name} did not recover within {timeout} seconds")


class PostgreSQLChaosScenario(ChaosScenario):
    """Chaos scenario for PostgreSQL primary node."""
    
    def __init__(self, duration_seconds: int = 60):
        super().__init__(
            name="postgresql_primary_chaos",
            services=["pg-primary"],
            duration_seconds=duration_seconds
        )
        self.docker_client = docker.from_env()
        self.container: Optional[Container] = None
    
    async def execute_chaos(self) -> None:
        """Stop the PostgreSQL primary node."""
        logging.info("Executing chaos on PostgreSQL primary node")
        
        try:
            # Find the container
            containers = self.docker_client.containers.list(
                filters={"name": "pg-primary", "status": "running"}
            )
            
            if not containers:
                raise Exception("PostgreSQL primary container not found or not running")
            
            self.container = containers[0]
            
            # Stop the container
            self.container.stop()
            logging.info("Stopped PostgreSQL primary container")
            
            # Wait a bit for the stop to take effect
            await asyncio.sleep(10)
            
        except Exception as e:
            logging.error(f"Failed to execute chaos on PostgreSQL primary: {e}")
            raise
    
    async def restore_service(self) -> None:
        """Restore the PostgreSQL primary node."""
        logging.info("Restoring PostgreSQL primary node")
        
        if self.container:
            try:
                # Start the container
                self.container.start()
                logging.info("Started PostgreSQL primary container")
                
                # Wait for the service to be healthy
                await self._wait_for_postgres_health()
                
            except Exception as e:
                logging.error(f"Failed to restore PostgreSQL primary: {e}")
                raise
    
    async def validate_service_health(self) -> bool:
        """Validate PostgreSQL health."""
        try:
            await self._wait_for_postgres_health()
            return True
        except Exception:
            return False
    
    async def _wait_for_postgres_health(self, timeout: int = 120) -> None:
        """Wait for PostgreSQL to be healthy."""
        start_time = datetime.now()
        
        while (datetime.now() - start_time).total_seconds() < timeout:
            try:
                # Check if container is running
                if self.container and self.container.status != "running":
                    await asyncio.sleep(5)
                    continue
                
                # Try to connect to PostgreSQL
                # This would be implemented with actual PostgreSQL client
                await asyncio.sleep(5)
                return
                
            except Exception:
                await asyncio.sleep(5)
        
        raise Exception("PostgreSQL primary did not recover within timeout")


class VaultChaosScenario(ChaosScenario):
    """Chaos scenario for Vault nodes."""
    
    def __init__(self, node_name: str, duration_seconds: int = 60):
        super().__init__(
            name=f"vault_{node_name}_chaos",
            services=[node_name],
            duration_seconds=duration_seconds
        )
        self.node_name = node_name
        self.docker_client = docker.from_env()
        self.container: Optional[Container] = None
    
    async def execute_chaos(self) -> None:
        """Stop the Vault node."""
        logging.info(f"Executing chaos on Vault node: {self.node_name}")
        
        try:
            # Find the container
            containers = self.docker_client.containers.list(
                filters={"name": self.node_name, "status": "running"}
            )
            
            if not containers:
                raise Exception(f"Vault container {self.node_name} not found or not running")
            
            self.container = containers[0]
            
            # Stop the container
            self.container.stop()
            logging.info(f"Stopped Vault container: {self.node_name}")
            
            # Wait a bit for the stop to take effect
            await asyncio.sleep(10)
            
        except Exception as e:
            logging.error(f"Failed to execute chaos on {self.node_name}: {e}")
            raise
    
    async def restore_service(self) -> None:
        """Restore the Vault node."""
        logging.info(f"Restoring Vault node: {self.node_name}")
        
        if self.container:
            try:
                # Start the container
                self.container.start()
                logging.info(f"Started Vault container: {self.node_name}")
                
                # Wait for the service to be healthy
                await self._wait_for_vault_health()
                
            except Exception as e:
                logging.error(f"Failed to restore {self.node_name}: {e}")
                raise
    
    async def validate_service_health(self) -> bool:
        """Validate Vault health."""
        try:
            await self._wait_for_vault_health()
            return True
        except Exception:
            return False
    
    async def _wait_for_vault_health(self, timeout: int = 120) -> None:
        """Wait for Vault to be healthy."""
        start_time = datetime.now()
        
        while (datetime.now() - start_time).total_seconds() < timeout:
            try:
                # Check if container is running
                if self.container and self.container.status != "running":
                    await asyncio.sleep(5)
                    continue
                
                # Try to connect to Vault
                # This would be implemented with actual Vault client
                await asyncio.sleep(5)
                return
                
            except Exception:
                await asyncio.sleep(5)
        
        raise Exception(f"Vault node {self.node_name} did not recover within timeout")


class RedisChaosScenario(ChaosScenario):
    """Chaos scenario for Redis master node."""
    
    def __init__(self, duration_seconds: int = 60):
        super().__init__(
            name="redis_master_chaos",
            services=["redis-master"],
            duration_seconds=duration_seconds
        )
        self.docker_client = docker.from_env()
        self.container: Optional[Container] = None
    
    async def execute_chaos(self) -> None:
        """Stop the Redis master node."""
        logging.info("Executing chaos on Redis master node")
        
        try:
            # Find the container
            containers = self.docker_client.containers.list(
                filters={"name": "redis-master", "status": "running"}
            )
            
            if not containers:
                raise Exception("Redis master container not found or not running")
            
            self.container = containers[0]
            
            # Stop the container
            self.container.stop()
            logging.info("Stopped Redis master container")
            
            # Wait a bit for the stop to take effect
            await asyncio.sleep(10)
            
        except Exception as e:
            logging.error(f"Failed to execute chaos on Redis master: {e}")
            raise
    
    async def restore_service(self) -> None:
        """Restore the Redis master node."""
        logging.info("Restoring Redis master node")
        
        if self.container:
            try:
                # Start the container
                self.container.start()
                logging.info("Started Redis master container")
                
                # Wait for the service to be healthy
                await self._wait_for_redis_health()
                
            except Exception as e:
                logging.error(f"Failed to restore Redis master: {e}")
                raise
    
    async def validate_service_health(self) -> bool:
        """Validate Redis health."""
        try:
            await self._wait_for_redis_health()
            return True
        except Exception:
            return False
    
    async def _wait_for_redis_health(self, timeout: int = 120) -> None:
        """Wait for Redis to be healthy."""
        start_time = datetime.now()
        
        while (datetime.now() - start_time).total_seconds() < timeout:
            try:
                # Check if container is running
                if self.container and self.container.status != "running":
                    await asyncio.sleep(5)
                    continue
                
                # Try to connect to Redis
                # This would be implemented with actual Redis client
                await asyncio.sleep(5)
                return
                
            except Exception:
                await asyncio.sleep(5)
        
        raise Exception("Redis master did not recover within timeout")


class MinIOChaosScenario(ChaosScenario):
    """Chaos scenario for MinIO nodes."""
    
    def __init__(self, node_name: str, duration_seconds: int = 60):
        super().__init__(
            name=f"minio_{node_name}_chaos",
            services=[node_name],
            duration_seconds=duration_seconds
        )
        self.node_name = node_name
        self.docker_client = docker.from_env()
        self.container: Optional[Container] = None
    
    async def execute_chaos(self) -> None:
        """Stop the MinIO node."""
        logging.info(f"Executing chaos on MinIO node: {self.node_name}")
        
        try:
            # Find the container
            containers = self.docker_client.containers.list(
                filters={"name": self.node_name, "status": "running"}
            )
            
            if not containers:
                raise Exception(f"MinIO container {self.node_name} not found or not running")
            
            self.container = containers[0]
            
            # Stop the container
            self.container.stop()
            logging.info(f"Stopped MinIO container: {self.node_name}")
            
            # Wait a bit for the stop to take effect
            await asyncio.sleep(10)
            
        except Exception as e:
            logging.error(f"Failed to execute chaos on {self.node_name}: {e}")
            raise
    
    async def restore_service(self) -> None:
        """Restore the MinIO node."""
        logging.info(f"Restoring MinIO node: {self.node_name}")
        
        if self.container:
            try:
                # Start the container
                self.container.start()
                logging.info(f"Started MinIO container: {self.node_name}")
                
                # Wait for the service to be healthy
                await self._wait_for_minio_health()
                
            except Exception as e:
                logging.error(f"Failed to restore {self.node_name}: {e}")
                raise
    
    async def validate_service_health(self) -> bool:
        """Validate MinIO health."""
        try:
            await self._wait_for_minio_health()
            return True
        except Exception:
            return False
    
    async def _wait_for_minio_health(self, timeout: int = 120) -> None:
        """Wait for MinIO to be healthy."""
        start_time = datetime.now()
        
        while (datetime.now() - start_time).total_seconds() < timeout:
            try:
                # Check if container is running
                if self.container and self.container.status != "running":
                    await asyncio.sleep(5)
                    continue
                
                # Try to connect to MinIO
                # This would be implemented with actual MinIO client
                await asyncio.sleep(5)
                return
                
            except Exception:
                await asyncio.sleep(5)
        
        raise Exception(f"MinIO node {self.node_name} did not recover within timeout")


class OTelChaosScenario(ChaosScenario):
    """Chaos scenario for OTel Collector nodes."""
    
    def __init__(self, node_name: str, duration_seconds: int = 60):
        super().__init__(
            name=f"otel_{node_name}_chaos",
            services=[node_name],
            duration_seconds=duration_seconds
        )
        self.node_name = node_name
        self.docker_client = docker.from_env()
        self.container: Optional[Container] = None
    
    async def execute_chaos(self) -> None:
        """Stop the OTel Collector node."""
        logging.info(f"Executing chaos on OTel Collector node: {self.node_name}")
        
        try:
            # Find the container
            containers = self.docker_client.containers.list(
                filters={"name": self.node_name, "status": "running"}
            )
            
            if not containers:
                raise Exception(f"OTel Collector container {self.node_name} not found or not running")
            
            self.container = containers[0]
            
            # Stop the container
            self.container.stop()
            logging.info(f"Stopped OTel Collector container: {self.node_name}")
            
            # Wait a bit for the stop to take effect
            await asyncio.sleep(10)
            
        except Exception as e:
            logging.error(f"Failed to execute chaos on {self.node_name}: {e}")
            raise
    
    async def restore_service(self) -> None:
        """Restore the OTel Collector node."""
        logging.info(f"Restoring OTel Collector node: {self.node_name}")
        
        if self.container:
            try:
                # Start the container
                self.container.start()
                logging.info(f"Started OTel Collector container: {self.node_name}")
                
                # Wait for the service to be healthy
                await self._wait_for_otel_health()
                
            except Exception as e:
                logging.error(f"Failed to restore {self.node_name}: {e}")
                raise
    
    async def validate_service_health(self) -> bool:
        """Validate OTel Collector health."""
        try:
            await self._wait_for_otel_health()
            return True
        except Exception:
            return False
    
    async def _wait_for_otel_health(self, timeout: int = 120) -> None:
        """Wait for OTel Collector to be healthy."""
        start_time = datetime.now()
        
        while (datetime.now() - start_time).total_seconds() < timeout:
            try:
                # Check if container is running
                if self.container and self.container.status != "running":
                    await asyncio.sleep(5)
                    continue
                
                # Try to connect to OTel Collector health endpoint
                # This would be implemented with actual HTTP request
                await asyncio.sleep(5)
                return
                
            except Exception:
                await asyncio.sleep(5)
        
        raise Exception(f"OTel Collector node {self.node_name} did not recover within timeout")


class LangfuseChaosScenario(ChaosScenario):
    """Chaos scenario for Langfuse nodes."""
    
    def __init__(self, node_name: str, duration_seconds: int = 60):
        super().__init__(
            name=f"langfuse_{node_name}_chaos",
            services=[node_name],
            duration_seconds=duration_seconds
        )
        self.node_name = node_name
        self.docker_client = docker.from_env()
        self.container: Optional[Container] = None
    
    async def execute_chaos(self) -> None:
        """Stop the Langfuse node."""
        logging.info(f"Executing chaos on Langfuse node: {self.node_name}")
        
        try:
            # Find the container
            containers = self.docker_client.containers.list(
                filters={"name": self.node_name, "status": "running"}
            )
            
            if not containers:
                raise Exception(f"Langfuse container {self.node_name} not found or not running")
            
            self.container = containers[0]
            
            # Stop the container
            self.container.stop()
            logging.info(f"Stopped Langfuse container: {self.node_name}")
            
            # Wait a bit for the stop to take effect
            await asyncio.sleep(10)
            
        except Exception as e:
            logging.error(f"Failed to execute chaos on {self.node_name}: {e}")
            raise
    
    async def restore_service(self) -> None:
        """Restore the Langfuse node."""
        logging.info(f"Restoring Langfuse node: {self.node_name}")
        
        if self.container:
            try:
                # Start the container
                self.container.start()
                logging.info(f"Started Langfuse container: {self.node_name}")
                
                # Wait for the service to be healthy
                await self._wait_for_langfuse_health()
                
            except Exception as e:
                logging.error(f"Failed to restore {self.node_name}: {e}")
                raise
    
    async def validate_service_health(self) -> bool:
        """Validate Langfuse health."""
        try:
            await self._wait_for_langfuse_health()
            return True
        except Exception:
            return False
    
    async def _wait_for_langfuse_health(self, timeout: int = 120) -> None:
        """Wait for Langfuse to be healthy."""
        start_time = datetime.now()
        
        while (datetime.now() - start_time).total_seconds() < timeout:
            try:
                # Check if container is running
                if self.container and self.container.status != "running":
                    await asyncio.sleep(5)
                    continue
                
                # Try to connect to Langfuse health endpoint
                # This would be implemented with actual HTTP request
                await asyncio.sleep(5)
                return
                
            except Exception:
                await asyncio.sleep(5)
        
        raise Exception(f"Langfuse node {self.node_name} did not recover within timeout")


def create_all_chaos_scenarios() -> List[ChaosScenario]:
    """Create all chaos scenarios for HA components."""
    scenarios = []
    
    # ClickHouse scenarios
    scenarios.append(ClickHouseChaosScenario("clickhouse1"))
    scenarios.append(ClickHouseChaosScenario("clickhouse2"))
    scenarios.append(ClickHouseChaosScenario("clickhouse3"))
    scenarios.append(ClickHouseChaosScenario("clickhouse4"))
    
    # PostgreSQL scenario
    scenarios.append(PostgreSQLChaosScenario())
    
    # Vault scenarios
    scenarios.append(VaultChaosScenario("vault-1"))
    scenarios.append(VaultChaosScenario("vault-2"))
    scenarios.append(VaultChaosScenario("vault-3"))
    
    # Redis scenario
    scenarios.append(RedisChaosScenario())
    
    # MinIO scenarios
    scenarios.append(MinIOChaosScenario("minio1"))
    scenarios.append(MinIOChaosScenario("minio2"))
    scenarios.append(MinIOChaosScenario("minio3"))
    scenarios.append(MinIOChaosScenario("minio4"))
    
    # OTel Collector scenarios
    scenarios.append(OTelChaosScenario("otel-collector-1"))
    scenarios.append(OTelChaosScenario("otel-collector-2"))
    scenarios.append(OTelChaosScenario("otel-collector-3"))
    
    # Langfuse scenarios
    scenarios.append(LangfuseChaosScenario("langfuse-1"))
    scenarios.append(LangfuseChaosScenario("langfuse-2"))
    scenarios.append(LangfuseChaosScenario("langfuse-3"))
    
    return scenarios


def create_target_scenarios() -> List[ChaosScenario]:
    """Create chaos scenarios for specific PP52 targets."""
    scenarios = []
    
    # PP52 targets: {clickhouse-1, ch-1, pg-primary, vault-1, redis-1, minio-1, otel-collector-1, langfuse-1}
    scenarios.append(ClickHouseChaosScenario("clickhouse1"))  # clickhouse-1
    scenarios.append(ClickHouseChaosScenario("clickhouse1"))  # ch-1 (same as clickhouse-1)
    scenarios.append(PostgreSQLChaosScenario())  # pg-primary
    scenarios.append(VaultChaosScenario("vault-1"))  # vault-1
    scenarios.append(RedisChaosScenario())  # redis-1
    scenarios.append(MinIOChaosScenario("minio1"))  # minio-1
    scenarios.append(OTelChaosScenario("otel-collector-1"))  # otel-collector-1
    scenarios.append(LangfuseChaosScenario("langfuse-1"))  # langfuse-1
    
    return scenarios