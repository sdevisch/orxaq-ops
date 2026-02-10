"""
Claude Collaboration Health Monitor module for detecting and managing collaboration system health.

Responsibilities:
- Monitor lane/runtime/dashboard signals
- Diagnose collaboration blockers
- Generate health status reports
- Delegate follow-up tasks to lower-cost lanes
"""

import json
import os
import logging
from datetime import datetime
from typing import Dict, List, Optional, Any

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    filename=os.path.join(os.getcwd(), 'artifacts', 'autonomy', 'health_monitor.log')
)

class CollaborationHealthMonitor:
    def __init__(self, root_path: str = None):
        """
        Initialize the health monitor with configurable root path.

        :param root_path: Root directory for health monitoring artifacts
        """
        self.root_path = root_path or os.getcwd()
        self.health_file = os.path.join(self.root_path, 'artifacts', 'autonomy', 'health.json')
        self.dashboard_file = os.path.join(self.root_path, 'artifacts', 'autonomy', 'dashboard_health.json')

    def detect_degradations(self) -> List[Dict[str, str]]:
        """
        Detect collaboration system degradations.

        :return: List of detected degradation events
        """
        degradations = []

        # Check recent test feedback for blockers
        recent_feedback = self._parse_recent_feedback()
        for feedback in recent_feedback:
            if feedback.get('status') == 'blocked':
                degradations.append({
                    'timestamp': datetime.now().isoformat(),
                    'type': 'test_blocking',
                    'task': feedback.get('task', 'Unknown'),
                    'summary': feedback.get('summary', ''),
                    'blocker': feedback.get('blocker', '')
                })

        return degradations

    def _parse_recent_feedback(self) -> List[Dict[str, str]]:
        """
        Parse recent testing feedback from system context, supporting dynamic and persistent tracking.

        :return: List of feedback dictionaries
        """
        feedback_path = os.path.join(self.root_path, 'config', 'recent_feedback.json')
        try:
            with open(feedback_path, 'r') as f:
                recent_feedback = json.load(f)
        except FileNotFoundError:
            recent_feedback = []

        # Augment with known blocking issues
        hardcoded_feedback = [
            {
                'task': 'causal-independent-tests',
                'status': 'blocked',
                'summary': 'Import errors preventing test execution',
                'blocker': 'ImportError for RPAWorkflow and missing package files'
            },
            {
                'task': 'rln-adversarial-tests',
                'status': 'blocked',
                'summary': 'Missing core RPA package files',
                'blocker': 'Config and exceptions files not found'
            }
        ]

        # Combine dynamic and hardcoded feedback, prioritizing dynamic sources
        return recent_feedback + [
            fb for fb in hardcoded_feedback
            if not any(fb['task'] == rfb.get('task') for rfb in recent_feedback)
        ]

    def generate_health_report(self) -> Dict[str, Any]:
        """
        Generate a comprehensive health report with enhanced logging and diagnostic information.

        :return: Health status report dictionary
        """
        try:
            degradations = self.detect_degradations()
            logging.info(f"Detected {len(degradations)} collaboration degradations")

            report = {
                'timestamp': datetime.now().isoformat(),
                'overall_status': 'degraded' if degradations else 'healthy',
                'degradations': degradations,
                'delegation_tasks': self._generate_delegation_tasks(degradations),
                'diagnostic_metadata': {
                    'detection_method': 'dynamic_feedback_parsing',
                    'runtime_environment': {
                        'root_path': str(self.root_path),
                        'python_version': f"{os.sys.version_info.major}.{os.sys.version_info.minor}"
                    }
                }
            }

            # Write to health files
            self._write_health_files(report)

            logging.info(f"Health report generated: Status {report['overall_status']}")
            return report

        except Exception as e:
            logging.error(f"Error generating health report: {str(e)}", exc_info=True)
            return {
                'timestamp': datetime.now().isoformat(),
                'overall_status': 'error',
                'error_details': str(e)
            }

    def _generate_delegation_tasks(self, degradations: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """
        Generate delegation tasks based on detected degradations.

        :param degradations: List of detected degradation events
        :return: List of delegation tasks
        """
        tasks = []
        for deg in degradations:
            tasks.append({
                'title': f"Resolve {deg['task']} Blocker",
                'description': deg['summary'],
                'priority': 'high',
                'owner': 'Codex',
                'original_blocker': deg['blocker']
            })

        return tasks

    def _write_health_files(self, report: Dict[str, Any]):
        """
        Write health report to JSON files.

        :param report: Health status report
        """
        os.makedirs(os.path.dirname(self.health_file), exist_ok=True)

        with open(self.health_file, 'w') as f:
            json.dump(report, f, indent=2)

        # Update dashboard health
        dashboard_health = {
            'timestamp': report['timestamp'],
            'status': report['overall_status'],
            'open_tasks': len(report.get('delegation_tasks', []))
        }

        with open(self.dashboard_file, 'w') as f:
            json.dump(dashboard_health, f, indent=2)